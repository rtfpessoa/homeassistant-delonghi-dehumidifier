"""Exercise device-scoped restart fencing and shutdown availability."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_component
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api import config_flow, identity, repairs
from custom_components.delonghi_dehumidifier_api.client import APIClient, OffOnStatus
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_account_fence
from tests.test_multidevice_commands import posts

cloud = test_account_fence.cloud
provider = test_account_fence.provider
entry = test_account_fence.entry

SETTINGS = {
    CONF_EMAIL: "new@example.test",
    CONF_PASSWORD: "new-secret",
    CONF_LANGUAGE: "en",
}


async def begin_selection(hass):
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(flow["flow_id"], SETTINGS)


async def test_reenroll_after_partial_unload_fenced_other_device_works(
    hass, entry, cloud, provider
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    original_unload = hass.config_entries.async_unload_platforms
    remaining = ("binary_sensor", "humidifier", "sensor")

    async def partially_unload(config_entry, platforms):
        assert await original_unload(config_entry, ["switch"])
        return False

    try:
        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(side_effect=partially_unload),
        ):
            assert not await hass.config_entries.async_unload(entry.entry_id)
        assert identity.device_needs_restart(hass, "selected")
        assert (await hass.config_entries.async_remove(entry.entry_id))[
            "require_restart"
        ]
        await hass.async_block_till_done()
        with patch.object(
            APIClient, "get_devices", AsyncMock(return_value=["selected", "other"])
        ):
            flow = await begin_selection(hass)
            assert flow["step_id"] == "select_device"
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {identity.CONF_DSN: "selected"}
            )
            assert result["type"] == "abort"
            assert result["reason"] == "restart_required"
            assert not hass.config_entries.async_entries(DOMAIN)
            assert not posts(cloud)

            legacy, manager, repair = await begin_repair(hass)
            repair = await manager.async_configure(
                repair["flow_id"], {identity.CONF_DSN: "selected"}
            )
            assert repair["type"] == "abort"
            assert repair["reason"] == "restart_required"
            assert identity.CONF_DSN not in legacy.data
            claims = await identity.reload_journal(hass)
            assert (
                claims[identity.device_unique_id("selected")]["owner"] == entry.entry_id
            )
            assert not posts(cloud)

            # A different selected device is not held hostage by the old fence.
            flow = await begin_selection(hass)
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {identity.CONF_DSN: "other"}
            )
            assert result["type"] == "create_entry"
            await hass.async_block_till_done()
            other = result["result"]
            assert other.state is ConfigEntryState.LOADED
            await other.runtime_data.set_swing(OffOnStatus.ON)
            assert len(posts(cloud)) == 1
            assert "/dsns/other/" in posts(cloud)[0][1]
            assert await hass.config_entries.async_unload(other.entry_id)
    finally:
        # HA removed registry entities but retained platform instances after the
        # failed unload. Clean only those remaining instances in this test.
        components = hass.data[entity_component.DATA_INSTANCES]
        for domain in remaining:
            if entry.entry_id in components[domain]._platforms:
                components[domain]._platforms[entry.entry_id].async_unsub_polling()
                await components[domain].async_unload_entry(entry)
        await client.async_shutdown()


@pytest.mark.parametrize("gate_stage", ["journal", "unique_id"])
async def test_enrollment_rechecks_fence_after_await(hass, cloud, provider, gate_stage):
    started, resume = asyncio.Event(), asyncio.Event()
    original_journal = config_flow.reload_journal
    original_set_id = config_flow.DeLonghiConfigFlow.async_set_unique_id

    async def journal(*args):
        value = await original_journal(*args)
        if gate_stage == "journal":
            started.set()
            await resume.wait()
        return value

    async def set_id(flow, *args):
        value = await original_set_id(flow, *args)
        if gate_stage == "unique_id":
            started.set()
            await resume.wait()
        return value

    flow = await begin_selection(hass)
    with (
        patch.object(config_flow, "reload_journal", journal),
        patch.object(config_flow.DeLonghiConfigFlow, "async_set_unique_id", set_id),
    ):
        selection = asyncio.create_task(
            hass.config_entries.flow.async_configure(
                flow["flow_id"], {identity.CONF_DSN: "selected"}
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            identity.require_restart(hass, "old-entry", "selected")
            resume.set()
            result = await selection
            assert result["type"] == "abort"
            assert result["reason"] == "restart_required"
            assert not hass.config_entries.async_entries(DOMAIN)
            assert not hass.data.get(f"{DOMAIN}_enrollments")
            assert not posts(cloud)
        finally:
            resume.set()
            if not selection.done():
                await selection


async def test_shutdown_notifies_survivors_once_and_does_not_retain_listeners(caplog):
    client = APIClient(None, "en", "user", "secret")
    notified = Mock()
    failure = Mock(side_effect=RuntimeError("private listener details"))
    reauth = Mock()
    client.on_invalid_credentials = reauth
    remove = client.subscribe_auth_failure(notified)
    client.subscribe_auth_failure(failure)
    await client.async_shutdown()
    assert client.closed
    assert client.action_refresh._closed
    notified.assert_called_once()
    failure.assert_called_once()
    reauth.assert_not_called()
    assert not client._auth_listeners
    assert "private listener details" not in caplog.text
    remove()
    late = Mock()
    remove_late = client.subscribe_auth_failure(late)
    remove_late()
    await client.async_shutdown()
    notified.assert_called_once()
    late.assert_not_called()
    assert not client._auth_listeners


async def test_device_fence_appearing_during_command_read_is_not_an_ownership_issue(
    hass, entry, cloud, provider
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    started, resume = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def gated(func, *args):
        if func is identity._read_journal:
            started.set()
            await resume.wait()
        return await original(func, *args)

    with patch.object(hass, "async_add_executor_job", gated):
        command = asyncio.create_task(entry.runtime_data.set_swing(OffOnStatus.ON))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            identity.require_restart(hass, "old-entry", "selected")
            resume.set()
            with pytest.raises(HomeAssistantError, match="Restart Home Assistant"):
                await command
            assert not posts(cloud)
            assert not test_account_fence.binding_issue(hass, entry)
        finally:
            resume.set()
            if not command.done():
                await command
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_device_fence_stops_setup_before_cloud_reads(
    hass, entry, cloud, provider
):
    identity.require_restart(hass, "old-entry", "selected")
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth:
        assert not await hass.config_entries.async_setup(entry.entry_id)
    auth.assert_not_awaited()
    assert not posts(cloud)
    assert not test_account_fence.binding_issue(hass, entry)


async def begin_repair(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    legacy = MockConfigEntry(domain=DOMAIN, data=SETTINGS, version=2)
    legacy.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=legacy.entry_id,
        identifiers={(DOMAIN, "selected")},
        name="Dehumidifier",
    )
    identity.create_binding_issue(hass, legacy)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    flow = await manager.async_init(
        DOMAIN, data={"issue_id": f"device_binding_{legacy.entry_id}"}
    )
    return legacy, manager, flow


async def test_existing_fence_blocks_repairs_selection(hass, cloud, provider):
    legacy, manager, flow = await begin_repair(hass)
    identity.require_restart(hass, "old-entry", "selected")
    result = await manager.async_configure(
        flow["flow_id"], {identity.CONF_DSN: "selected"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "restart_required"
    assert identity.CONF_DSN not in legacy.data
    assert not posts(cloud)


@pytest.mark.parametrize("fence_during_read", [False, True])
async def test_repair_owner_journal_failure_is_retryable_or_requires_restart(
    hass, cloud, provider, fence_during_read, caplog
):
    legacy, manager, flow = await begin_repair(hass)
    flow = await manager.async_configure(
        flow["flow_id"], {identity.CONF_DSN: "selected"}
    )
    started, resume = asyncio.Event(), asyncio.Event()

    async def fail_read(hass):
        started.set()
        await resume.wait()
        raise identity.ClaimJournalError("private journal details")

    with patch.object(repairs, "load_journal", fail_read):
        owner = asyncio.create_task(
            manager.async_configure(flow["flow_id"], {"owner": legacy.entry_id})
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if fence_during_read:
                identity.require_restart(hass, "old-entry", "selected")
            resume.set()
            result = await owner
            if fence_during_read:
                assert result["type"] == "abort"
                assert result["reason"] == "restart_required"
            else:
                assert result["type"] == "form"
                assert result["step_id"] == "init"
                assert result["errors"]["base"] == "ownership_unavailable"
                manager.async_abort(result["flow_id"])
            assert legacy.data == SETTINGS
            assert legacy.unique_id is None
            assert not posts(cloud)
            assert "private journal details" not in caplog.text
        finally:
            resume.set()
            if not owner.done():
                await owner


async def test_repair_owner_journal_failure_after_entry_removed(hass, cloud, provider):
    from homeassistant.data_entry_flow import UnknownFlow

    legacy, manager, flow = await begin_repair(hass)
    flow = await manager.async_configure(
        flow["flow_id"], {identity.CONF_DSN: "selected"}
    )
    started, resume = asyncio.Event(), asyncio.Event()

    async def fail_read(hass):
        started.set()
        await resume.wait()
        raise identity.ClaimJournalError("private journal details")

    with patch.object(repairs, "load_journal", fail_read):
        owner = asyncio.create_task(
            manager.async_configure(flow["flow_id"], {"owner": legacy.entry_id})
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await hass.config_entries.async_remove(legacy.entry_id)
            resume.set()
            try:
                result = await owner
            except UnknownFlow:
                pass  # HA may abort an in-progress repair as it removes the entry.
            else:
                assert result["type"] == "abort"
                assert result["reason"] == "entry_removed"
            assert hass.config_entries.async_get_entry(legacy.entry_id) is None
            assert not posts(cloud)
        finally:
            resume.set()
            if not owner.done():
                await owner


@pytest.mark.parametrize("gate_stage", ["journal", "election_write"])
async def test_repair_rechecks_fence_during_election(hass, cloud, provider, gate_stage):
    legacy, manager, flow = await begin_repair(hass)
    flow = await manager.async_configure(
        flow["flow_id"], {identity.CONF_DSN: "selected"}
    )
    flow = await manager.async_configure(flow["flow_id"], {"owner": legacy.entry_id})
    assert flow["step_id"] == "confirm"
    started, resume = asyncio.Event(), asyncio.Event()
    original_journal = repairs.reload_journal
    original_save = repairs.save_election

    async def journal(*args):
        value = await original_journal(*args)
        if gate_stage == "journal":
            started.set()
            await resume.wait()
        return value

    async def save(*args):
        await original_save(*args)
        if gate_stage == "election_write":
            started.set()
            await resume.wait()

    with (
        patch.object(repairs, "reload_journal", journal),
        patch.object(repairs, "save_election", save),
    ):
        confirmation = asyncio.create_task(manager.async_configure(flow["flow_id"], {}))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            identity.require_restart(hass, "old-entry", "selected")
            resume.set()
            result = await confirmation
            assert result["type"] == "abort"
            assert result["reason"] == "restart_required"
            assert identity.CONF_DSN not in legacy.data
            assert legacy.unique_id is None
            assert not posts(cloud)
            claims = await identity.reload_journal(hass)
            claim = claims.get(identity.device_unique_id("selected"))
            if gate_stage == "election_write":
                assert claim["owner"] == legacy.entry_id
            else:
                assert claim is None
        finally:
            resume.set()
            if not confirmation.done():
                await confirmation


@patch("homeassistant.helpers.device_registry.CLEANUP_DELAY", 0)
async def test_removed_device_can_be_enrolled_after_fresh_ha_restart(tmp_path):
    from tests.identity_helpers import bound_entry
    from tests.test_identity_restart_rollback import (
        DATA,
        FakeCloud,
        fresh_home_assistant,
        save_home_assistant,
    )

    fake_cloud = FakeCloud()
    async with fake_cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            old = bound_entry(domain=DOMAIN, dsn="a", data=DATA)
            old.add_to_hass(hass)
            assert await hass.config_entries.async_setup(old.entry_id)
            client = old.runtime_data
            original_unload = hass.config_entries.async_unload_platforms

            async def partially_unload(config_entry, platforms):
                assert await original_unload(config_entry, ["switch"])
                return False

            with patch.object(
                hass.config_entries,
                "async_unload_platforms",
                AsyncMock(side_effect=partially_unload),
            ):
                assert not await hass.config_entries.async_unload(old.entry_id)
            assert identity.device_needs_restart(hass, "a")
            assert (await hass.config_entries.async_remove(old.entry_id))[
                "require_restart"
            ]
            await hass.async_block_till_done()
            await save_home_assistant(hass)
            components = hass.data[entity_component.DATA_INSTANCES]
            for domain in ("binary_sensor", "humidifier", "sensor"):
                if old.entry_id in components[domain]._platforms:
                    components[domain]._platforms[old.entry_id].async_unsub_polling()
                    await components[domain].async_unload_entry(old)
            await client.async_shutdown()
        async with fresh_home_assistant(tmp_path) as hass:
            assert not identity.device_needs_restart(hass, "a")
            flow = await begin_selection(hass)
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"], {identity.CONF_DSN: "a"}
            )
            assert result["type"] == "create_entry"
            await hass.async_block_till_done()
            replacement = result["result"]
            assert replacement.state is ConfigEntryState.LOADED
            await replacement.runtime_data.set_swing(OffOnStatus.ON)
            assert len(fake_cloud.posts) == 1
            assert await hass.config_entries.async_unload(replacement.entry_id)
