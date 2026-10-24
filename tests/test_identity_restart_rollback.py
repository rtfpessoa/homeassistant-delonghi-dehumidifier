"""Exercise identity recovery across Home Assistant restart and rollback."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import bootstrap, loader
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers import restore_state as rs
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    flush_store,
)

from custom_components.delonghi_dehumidifier_api import repairs
from custom_components.delonghi_dehumidifier_api.client import APIClient, Status
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import CONF_DSN
from tests.identity_helpers import (
    assert_device_associations,
    assert_pinned_identity,
)
from tests.test_multidevice_commands import Response

DATA = {
    CONF_EMAIL: "person@example.invalid",
    CONF_PASSWORD: "password",
    CONF_LANGUAGE: "en",
}


@asynccontextmanager
async def fresh_home_assistant(config_dir):
    """Load real registries and serialized config entries in a fresh HA instance."""
    async with async_test_home_assistant(
        load_registries=False, config_dir=str(config_dir)
    ) as hass:
        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        if hasattr(dr, "async_setup"):
            dr.async_setup(hass)
        for registry in (ar, cr, dr, er, fr, ir, lr, rs):
            await registry.async_load(hass)
        hass.data[bootstrap.DATA_REGISTRIES_LOADED] = None
        await hass.config_entries.async_initialize()
        try:
            yield hass
        finally:
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.state is ConfigEntryState.LOADED:
                    await hass.config_entries.async_unload(entry.entry_id)
                elif entry.state is ConfigEntryState.SETUP_RETRY:
                    entry.async_cancel_retry_setup()
            await hass.async_stop()


async def save_home_assistant(hass):
    """Flush HA's actual config-entry and registry storage serializers."""
    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    await flush_store(dr.async_get(hass)._store)
    await flush_store(er.async_get(hass)._store)


def seed_legacy_pair(hass, *, disabled_by=None):
    """Create two legacy associations with user-customized entities."""
    owner = MockConfigEntry(
        domain=DOMAIN, data=DATA, title="Winner", version=1, disabled_by=disabled_by
    )
    loser = MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        options={CONF_PASSWORD: "override"},
        title="Other",
        version=1,
        disabled_by=disabled_by,
    )
    owner.add_to_hass(hass)
    loser.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={(DOMAIN, "a")}, name="Appliance"
    )
    devices.async_get_or_create(
        config_entry_id=loser.entry_id, identifiers={(DOMAIN, "a")}
    )
    device = devices.async_update_device(device.id, name_by_user="My device")
    entities = er.async_get(hass)
    active = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_a_target_humidity_sensor",
        config_entry=loser,
        device_id=device.id,
    )
    active = entities.async_update_entity(
        active.entity_id,
        new_entity_id="sensor.my_target",
        name="My target",
        icon="mdi:star",
    )
    disabled = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_a_current_humidity_sensor",
        config_entry=loser,
        device_id=device.id,
    )
    disabled = entities.async_update_entity(
        disabled.entity_id,
        new_entity_id="sensor.my_humidity",
        name="My humidity",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    return owner, loser, device, active, disabled


class FakeCloud:
    """Record provider device reads and actual HTTP device writes."""

    def __init__(self):
        self.devices = ["a", "b"]
        self.reads = []
        self.posts = []
        self.values = {
            "appliance_model": "DDSX220",
            "firmware_version": "1",
            "hardware_version": "1",
            "current_humidity": 50,
            "humidity_setpoint": 55,
            "current_speed": 3,
            "device_mode": 1,
            "device_status": 1,
            "filter_status": 1,
            "filter_change_alarm": 0,
            "filter_life": 90,
            "room_temp": 210,
            "heat_exchanger_temp": 220,
            "set_eco": 0,
            "swing": 0,
            "alarm_state": 0,
        }

    async def get_request(self, path):
        self.reads.append(path)
        if path == "apiv1/devices.json":
            return [{"device": {"dsn": dsn}} for dsn in self.devices]
        assert path == "apiv1/dsns/a/properties.json"
        return [
            {"property": {"name": key, "value": value, "product_name": "Test"}}
            for key, value in self.values.items()
        ]

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response(201, {})

    @asynccontextmanager
    async def patched(self):
        session = Mock()
        session.post = self.post
        with (
            patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
            patch.object(
                APIClient, "get_access_token", AsyncMock(return_value="token")
            ),
            patch.object(
                APIClient, "get_request", AsyncMock(side_effect=self.get_request)
            ),
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=session,
            ),
        ):
            yield


async def confirm_owner(hass, issue_entry, owner):
    """Complete the actual Repairs flow in a retrying config entry."""
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    flow = await manager.async_init(
        DOMAIN, data={"issue_id": f"device_binding_{issue_entry}"}
    )
    flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
    flow = await manager.async_configure(flow["flow_id"], {"owner": owner})
    return await manager.async_configure(flow["flow_id"], {})


async def confirm_owner_loaded(hass, owner):
    """Confirm an election and wait until the chosen entry is loaded."""
    result = await confirm_owner(hass, owner.entry_id, owner.entry_id)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert owner.state is ConfigEntryState.LOADED


def assert_election_binding(owner, loser, dsn):
    """Verify the election pins only the chosen entry."""
    assert owner.data[CONF_DSN] == dsn
    assert CONF_DSN not in loser.data


def assert_user_customizations(hass, device, active, disabled):
    """Check the user-customized device and entities survived serialization."""
    assert dr.async_get(hass).async_get(device.id).name_by_user == "My device"
    entities = er.async_get(hass)
    assert_restored_active_entity(entities, active.entity_id, active)
    restored = entities.async_get(disabled.entity_id)
    assert (restored.id, restored.name, restored.disabled_by) == (
        disabled.id,
        "My humidity",
        er.RegistryEntryDisabler.USER,
    )


def assert_restored_active_entity(entities, active_id, active):
    """Check the renamed and customized active entity retains its identity."""
    restored = entities.async_get(active_id)
    assert (restored.id, restored.name, restored.icon) == (
        active.id,
        "My target",
        "mdi:star",
    )


def assert_unloaded_without_cloud_io(owner, loser, cloud):
    """Verify disabled entries stayed unloaded and made no cloud requests."""
    assert owner.state is loser.state is ConfigEntryState.NOT_LOADED
    assert not cloud.reads and not cloud.posts


async def test_election_survives_restart_with_registry_and_fenced_loser(tmp_path):
    cloud = FakeCloud()
    async with cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            owner, loser, device, active, disabled = seed_legacy_pair(hass)
            assert not await hass.config_entries.async_setup(owner.entry_id)
            if loser.state is ConfigEntryState.NOT_LOADED:
                assert not await hass.config_entries.async_setup(loser.entry_id)
            assert loser.state is ConfigEntryState.SETUP_RETRY
            await confirm_owner_loaded(hass, owner)
            assert_election_binding(owner, loser, "a")
            assert not cloud.posts
            await save_home_assistant(hass)
            owner_id, loser_id = owner.entry_id, loser.entry_id
            device_id, active_id = device.id, active.entity_id
            loser_devices = {
                item.id
                for item in dr.async_entries_for_config_entry(
                    dr.async_get(hass), loser_id
                )
            }

        cloud.devices.reverse()
        cloud.reads.clear()
        async with fresh_home_assistant(tmp_path) as hass:
            owner = hass.config_entries.async_get_entry(owner_id)
            loser = hass.config_entries.async_get_entry(loser_id)
            assert_election_binding(owner, loser, "a")
            assert loser.options[CONF_PASSWORD] == "override"
            devices = dr.async_get(hass)
            assert_user_customizations(hass, device, active, disabled)
            assert_device_associations(devices, owner_id, {device_id})
            assert_device_associations(devices, loser_id, loser_devices)
            entities = er.async_get(hass)
            assert not await hass.config_entries.async_setup(loser_id)
            assert loser.state is ConfigEntryState.SETUP_RETRY
            if owner.state is ConfigEntryState.NOT_LOADED:
                assert await hass.config_entries.async_setup(owner_id)
            await hass.async_block_till_done()
            assert_restored_active_entity(entities, active_id, active)
            assert_pinned_identity(owner, "a")
            assert cloud.reads and not any("/dsns/b/" in path for path in cloud.reads)
            assert not cloud.posts
            await owner.runtime_data.set_status(Status.ON)
            assert len(cloud.posts) == 1
            assert "/dsns/a/" in cloud.posts[0][0]


async def test_interrupted_election_stays_blocked_until_confirmed(tmp_path):
    cloud = FakeCloud()
    async with cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            owner, loser, device, active, disabled = seed_legacy_pair(hass)
            assert not await hass.config_entries.async_setup(owner.entry_id)
            save_election = repairs.save_election

            async def interrupt_after_durable_write(*args, **kwargs):
                await save_election(*args, **kwargs)
                raise asyncio.CancelledError

            with (
                patch.object(
                    repairs, "save_election", side_effect=interrupt_after_durable_write
                ),
                pytest.raises(asyncio.CancelledError),
            ):
                await confirm_owner(hass, owner.entry_id, owner.entry_id)
            assert CONF_DSN not in owner.data
            assert CONF_DSN not in loser.data
            assert (tmp_path / ".storage" / f"{DOMAIN}.claims").is_file()
            assert not cloud.posts
            await save_home_assistant(hass)
            owner_id, loser_id = owner.entry_id, loser.entry_id

        cloud.reads.clear()
        async with fresh_home_assistant(tmp_path) as hass:
            owner = hass.config_entries.async_get_entry(owner_id)
            loser = hass.config_entries.async_get_entry(loser_id)
            assert not await hass.config_entries.async_setup(owner_id)
            if loser.state is ConfigEntryState.NOT_LOADED:
                assert not await hass.config_entries.async_setup(loser_id)
            assert owner.state is ConfigEntryState.SETUP_RETRY
            assert loser.state is ConfigEntryState.SETUP_RETRY
            assert not cloud.reads and not cloud.posts
            assert_user_customizations(hass, device, active, disabled)
            await confirm_owner_loaded(hass, owner)
            assert loser.state is ConfigEntryState.SETUP_RETRY
            assert not cloud.posts


@pytest.mark.parametrize(
    "damage", ["deleted", "claim_missing", "wrong_owner", "corrupt"]
)
async def test_journal_mismatch_fences_loaded_and_restarted_owner(tmp_path, damage):
    cloud = FakeCloud()
    async with cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            owner, loser, _, _, _ = seed_legacy_pair(hass)
            assert not await hass.config_entries.async_setup(owner.entry_id)
            await confirm_owner_loaded(hass, owner)
            path = tmp_path / ".storage" / f"{DOMAIN}.claims"
            if damage == "deleted":
                path.unlink()
            elif damage == "corrupt":
                path.write_text("{bad", encoding="utf-8")
            else:
                content = json.loads(path.read_text(encoding="utf-8"))
                if damage == "claim_missing":
                    content["claims"].clear()
                else:
                    record = next(iter(content["claims"].values()))
                    record.update(owner=loser.entry_id, losers=[owner.entry_id])
                path.write_text(json.dumps(content), encoding="utf-8")
            with pytest.raises(HomeAssistantError):
                await owner.runtime_data.set_status(Status.ON)
            assert not cloud.posts
            assert ir.async_get(hass).async_get_issue(
                DOMAIN, f"device_binding_{owner.entry_id}"
            )
            await save_home_assistant(hass)
            owner_id, loser_id = owner.entry_id, loser.entry_id
            evidence = path.read_bytes() if path.exists() else None

        cloud.reads.clear()
        async with fresh_home_assistant(tmp_path) as hass:
            owner = hass.config_entries.async_get_entry(owner_id)
            assert not await hass.config_entries.async_setup(owner_id)
            assert owner.state is ConfigEntryState.SETUP_RETRY
            loser = hass.config_entries.async_get_entry(loser_id)
            if loser.state is ConfigEntryState.NOT_LOADED:
                assert not await hass.config_entries.async_setup(loser_id)
            assert loser.state is ConfigEntryState.SETUP_RETRY
            assert not cloud.reads and not cloud.posts
            assert owner.data[CONF_DSN] == "a"
            assert ir.async_get(hass).async_get_issue(
                DOMAIN, f"device_binding_{owner_id}"
            )
            assert (path.read_bytes() if path.exists() else None) == evidence


async def test_matching_backup_restore_stays_unloaded_after_reorder(
    tmp_path, hass_storage
):
    """A matching legacy restore preserves identities, but is not safe to load blindly."""
    cloud = FakeCloud()
    async with cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            owner, loser, device, active, disabled = seed_legacy_pair(
                hass, disabled_by=ConfigEntryDisabler.USER
            )
            await save_home_assistant(hass)
            owner_id, loser_id = owner.entry_id, loser.entry_id

        # Verify the disabled flags survived HA serialization before making the
        # backup. Merely unloading before restoring an enabled backup is unsafe.
        async with fresh_home_assistant(tmp_path) as hass:
            owner = hass.config_entries.async_get_entry(owner_id)
            loser = hass.config_entries.async_get_entry(loser_id)
            assert owner.disabled_by is loser.disabled_by is ConfigEntryDisabler.USER
            assert await async_setup_component(hass, DOMAIN, {})
            await hass.async_block_till_done()
            assert_unloaded_without_cloud_io(owner, loser, cloud)
            await save_home_assistant(hass)
            backup = copy.deepcopy(hass_storage)
            await hass.config_entries.async_set_disabled_by(owner_id, None)
            await hass.config_entries.async_set_disabled_by(loser_id, None)
            if owner.state is ConfigEntryState.NOT_LOADED:
                assert not await hass.config_entries.async_setup(owner_id)
            assert owner.state is ConfigEntryState.SETUP_RETRY
            await confirm_owner_loaded(hass, owner)
            await save_home_assistant(hass)
            assert await hass.config_entries.async_unload(owner_id)
            if loser.state is ConfigEntryState.SETUP_RETRY:
                loser.async_cancel_retry_setup()
            assert not cloud.posts

        cloud.devices.reverse()
        hass_storage.clear()
        hass_storage.update(copy.deepcopy(backup))
        (tmp_path / ".storage" / f"{DOMAIN}.claims").unlink()
        cloud.reads.clear()
        for _ in range(2):
            async with fresh_home_assistant(tmp_path) as hass:
                owner = hass.config_entries.async_get_entry(owner_id)
                loser = hass.config_entries.async_get_entry(loser_id)
                assert owner.version == loser.version == 1
                assert dict(owner.data) == DATA and CONF_DSN not in owner.data
                assert loser.options[CONF_PASSWORD] == "override"
                assert (
                    owner.disabled_by is loser.disabled_by is ConfigEntryDisabler.USER
                )
                assert await async_setup_component(hass, DOMAIN, {})
                await hass.async_block_till_done()
                assert_unloaded_without_cloud_io(owner, loser, cloud)
                assert_user_customizations(hass, device, active, disabled)
                await save_home_assistant(hass)
