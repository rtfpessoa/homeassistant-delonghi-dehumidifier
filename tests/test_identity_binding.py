"""Exercise device identity at the real Home Assistant config-entry boundary."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    DeviceNotSelectedError,
    InvalidDeviceListError,
    NoDevicesError,
    Status,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import (
    CONF_DSN,
    device_unique_id,
)
from tests.identity_helpers import (
    assert_device_associations,
    assert_legacy_binding_pending,
    assert_pinned_identity,
)

DATA = {
    CONF_EMAIL: "person@example.org",
    CONF_PASSWORD: "password",
    CONF_LANGUAGE: "en",
}


def assert_legacy_entry_unchanged(entry):
    """The failed repair has not changed credentials or pinned the entry."""
    assert entry.data == DATA
    assert entry.options == {}
    assert entry.unique_id is None


@pytest.fixture(autouse=True)
def device_properties_for_identity_tests():
    """Identity tests do not exercise the initial device properties request."""
    with patch.object(APIClient, "get_properties", AsyncMock(return_value=[])):
        yield


@pytest.mark.parametrize(
    "body,error",
    [
        ([], NoDevicesError),
        ({}, InvalidDeviceListError),
        ([{}], InvalidDeviceListError),
        ([{"device": {"dsn": "x"}}, {"device": {"dsn": "x"}}], InvalidDeviceListError),
        ([{"device": {"dsn": ".."}}], InvalidDeviceListError),
    ],
)
async def test_discovery_rejects_invalid_data_without_selecting(body, error):
    client = APIClient(None, "en", "user", "password")
    client.get_request = AsyncMock(return_value=body)
    client.post_request = AsyncMock()
    with pytest.raises(error):
        await client.get_devices()
    with pytest.raises(DeviceNotSelectedError):
        await client.set_status(Status.ON)
    client.post_request.assert_not_called()
    await client.async_shutdown()


async def test_client_stays_on_verified_identity_when_order_changes():
    client = APIClient(None, "en", "user", "password")
    client.get_request = AsyncMock(
        side_effect=[
            [{"device": {"dsn": "a"}}, {"device": {"dsn": "b"}}],
            [{"device": {"dsn": "b"}}, {"device": {"dsn": "a"}}],
        ]
    )
    client.post_request = AsyncMock()
    await client.verify_device("a")
    assert await client.get_devices() == ["b", "a"]
    await client.set_status(Status.ON)
    assert client.post_request.call_args.args[0].startswith("apiv1/dsns/a/")
    await client.async_shutdown()


async def test_legacy_migration_and_setup_preserve_registry(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    entry = MockConfigEntry(
        domain=DOMAIN, data=DATA, options={CONF_PASSWORD: "override"}, version=1
    )
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "a")}, name="Custom name"
    )
    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    entities = er.async_get(hass)
    entity = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_a_humidity",
        config_entry=entry,
        device_id=device.id,
    )
    entity = entities.async_update_entity(
        entity.entity_id,
        new_entity_id="sensor.my_humidity",
        name="My humidity",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["b", "a"])
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await assert_legacy_binding_pending(hass, entry)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        assert flow["description_placeholders"]["devices"] == "Custom name"
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(flow["flow_id"], {"owner": entry.entry_id})
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["type"] == "create_entry"
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert_pinned_identity(entry, "a")
    assert entry.options[CONF_PASSWORD] == "override"
    assert registry.async_get(device.id).name == "Custom name"
    assert registry.async_get(device.id).disabled_by is dr.DeviceEntryDisabler.USER
    assert entities.async_get(entity.entity_id) == entity
    await entry.runtime_data.async_shutdown()


async def test_legacy_ambiguity_is_retryable_without_network(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    for dsn in ["a", "b"]:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, dsn)}
        )
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth:
        await assert_legacy_binding_pending(hass, entry)
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"device_binding_{entry.entry_id}"
    )
    auth.assert_not_called()


async def test_repairs_elects_owner_without_removing_loser_association(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    first = MockConfigEntry(domain=DOMAIN, title="First", data=DATA, version=1)
    second = MockConfigEntry(domain=DOMAIN, title="Second", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=first.entry_id, identifiers={(DOMAIN, "a")}
    )
    registry.async_get_or_create(
        config_entry_id=second.entry_id, identifiers={(DOMAIN, "a")}
    )
    associations = {
        entry.entry_id: {
            item.id
            for item in dr.async_entries_for_config_entry(registry, entry.entry_id)
        }
        for entry in (first, second)
    }
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await hass.config_entries.async_setup(first.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{first.entry_id}"}
        )
        assert form["step_id"] == "init"
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "a"})
        assert form["step_id"] == "owner"
        form = await manager.async_configure(form["flow_id"], {"owner": first.entry_id})
        assert form["step_id"] == "confirm"
        result = await manager.async_configure(form["flow_id"], {})
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
    assert first.data[CONF_DSN] == "a"
    assert first.state is ConfigEntryState.LOADED
    assert device_unique_id("a") in second.data["historical_device_claims"]
    assert registry.async_get(device.id) is not None
    for entry in (first, second):
        assert_device_associations(
            registry, entry.entry_id, associations[entry.entry_id]
        )
    assert CONF_DSN not in second.data


async def test_config_flow_selects_verified_device_and_rejects_duplicate(hass):
    import json
    from pathlib import Path

    from homeassistant.config_entries import SOURCE_USER

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a", "b"])
    ), patch.object(
        APIClient, "get_product_name", AsyncMock(return_value="Model")
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        assert result["step_id"] == "select_device"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DSN: "b"}
        )
        assert result["type"] == "create_entry"
        entry = result["result"]
        assert_pinned_identity(entry, "b")
        journal_path = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
        journal = json.loads(journal_path.read_text())
        assert journal["claims"][device_unique_id("b")]["owner"] == entry.entry_id
        assert journal_path.stat().st_mode & 0o077 == 0
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DSN: "b"}
        )
        assert result["type"] == "abort"
        assert result["reason"] == "already_configured"
        await entry.runtime_data.async_shutdown()


async def test_concurrent_config_flows_cannot_claim_same_device(hass):
    import asyncio

    from homeassistant.config_entries import SOURCE_USER

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a", "b"])
    ), patch.object(
        APIClient, "get_product_name", AsyncMock(return_value="Model")
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post, patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        flows = []
        for _ in range(2):
            flow = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.flow.async_configure(flow["flow_id"], DATA)
            assert flow["step_id"] == "select_device"
            flows.append(flow)
        results = await asyncio.gather(
            *(
                hass.config_entries.flow.async_configure(
                    flow["flow_id"], {CONF_DSN: "a"}
                )
                for flow in flows
            )
        )
        assert sorted(result["type"] for result in results) == ["abort", "create_entry"]
        entry = next(
            result["result"] for result in results if result["type"] == "create_entry"
        )
        assert entry.data[CONF_DSN] == "a"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        post.assert_not_called()
        await entry.runtime_data.async_shutdown()


async def test_interrupted_new_entry_requires_recovery_after_restart(hass):
    from homeassistant.config_entries import SOURCE_USER

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a"])
    ), patch.object(
        APIClient, "get_product_name", AsyncMock(return_value="Model")
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post, patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        assert result["step_id"] == "select_device"
        with patch(
            "custom_components.delonghi_dehumidifier_api.identity.save_json",
            side_effect=OSError("disk unavailable"),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_DSN: "a"}
            )
        entry = result["result"]
        assert entry.state is ConfigEntryState.SETUP_RETRY
        hass.data.pop(f"{DOMAIN}_enrollments", None)
        assert not await hass.config_entries.async_reload(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"device_binding_{entry.entry_id}"
        )
        post.assert_not_called()
        forward.assert_not_called()
        entry.async_cancel_retry_setup()


async def test_pinned_device_missing_never_rebinds_or_forwards(hass):
    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["b"])
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post, patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert entry.data[CONF_DSN] == "a"
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"device_binding_{entry.entry_id}"
        )
        post.assert_not_called()
        forward.assert_not_called()
        entry.async_cancel_retry_setup()


async def test_failed_options_do_not_change_account_or_binding(hass):
    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["b"])):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {**DATA, CONF_EMAIL: "other@example.org"}
        )
    assert flow["type"] == "form"
    assert flow["errors"]["base"] == "device_not_available"
    assert entry.data == {**DATA, CONF_DSN: "a"}
    assert entry.options == {}


@pytest.mark.parametrize(
    "failure", [NoDevicesError("no devices"), TransientCloudError()]
)
async def test_empty_discovery_retries_and_recovers_without_reauth(hass, failure):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient,
        "get_devices",
        AsyncMock(side_effect=[failure, ["a"]]),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        assert forward.await_count == 1
        assert len(entry.update_listeners) == 1
        await entry.runtime_data.async_shutdown()


async def test_repairs_translations_resolve_in_both_languages(hass):
    from homeassistant.helpers.translation import async_get_translations

    for language in ("en", "de"):
        translated = await async_get_translations(hass, language, "issues", [DOMAIN])
        prefix = f"component.{DOMAIN}.issues.device_binding"
        assert translated[f"{prefix}.title"]
        assert translated[f"{prefix}.fix_flow.step.init.data.device_dsn"]
        assert translated[f"{prefix}.fix_flow.step.owner.data.owner"]
        assert translated[f"{prefix}.fix_flow.step.confirm.description"]
        assert translated[f"{prefix}.fix_flow.error.device_not_available"]


async def test_repair_new_credentials_save_only_after_membership_verification(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        options={CONF_PASSWORD: "old-override", "keep": "value"},
        version=1,
    )
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient,
        "get_devices",
        AsyncMock(side_effect=[["missing", "a"], ["a"], ["a"], ["a"], ["a"]]),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)

        async def form_for(dsn):
            flow = await manager.async_init(
                DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
            )
            return await manager.async_configure(flow["flow_id"], {CONF_DSN: dsn})

        flow = await form_for("missing")
        flow = await manager.async_configure(
            flow["flow_id"], {"owner": entry.entry_id, CONF_PASSWORD: "new-password"}
        )
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["errors"]["base"] == "device_not_available"
        assert entry.data == DATA
        assert entry.options == {CONF_PASSWORD: "old-override", "keep": "value"}
        manager.async_abort(flow["flow_id"])
        flow = await form_for("a")
        flow = await manager.async_configure(
            flow["flow_id"], {"owner": entry.entry_id, CONF_PASSWORD: "new-password"}
        )
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["type"] == "create_entry"
        await hass.async_block_till_done()
        assert entry.data[CONF_PASSWORD] == "new-password"
        assert entry.options == {"keep": "value"}
        assert entry.state is ConfigEntryState.LOADED
        await entry.runtime_data.async_shutdown()


async def test_interrupted_election_is_blocked_after_journal_reload(hass):
    from custom_components.delonghi_dehumidifier_api.identity import (
        JOURNAL_KEY,
        binding_lock,
        save_election,
    )

    first = MockConfigEntry(domain=DOMAIN, title="First", data=DATA, version=1)
    second = MockConfigEntry(domain=DOMAIN, title="Second", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=first.entry_id, identifiers={(DOMAIN, "a")}
    )
    registry.async_get_or_create(
        config_entry_id=second.entry_id, identifiers={(DOMAIN, "a")}
    )
    # Simulate a process interruption after the durable precommit and before
    # any config-entry mutations. Reload only from the real on-disk journal.
    async with binding_lock(hass):
        await save_election(hass, "a", first.entry_id, {second.entry_id})
    hass.data.pop(JOURNAL_KEY)
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth, patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert not await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        assert first.state is ConfigEntryState.SETUP_RETRY
        assert second.state is ConfigEntryState.SETUP_RETRY
        assert CONF_DSN not in first.data and CONF_DSN not in second.data
        auth.assert_not_called()
        post.assert_not_called()
        first.async_cancel_retry_setup()
        second.async_cancel_retry_setup()


async def test_corrupt_election_journal_blocks_setup(hass):
    from pathlib import Path

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    path = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{invalid", encoding="utf-8")
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth, patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        auth.assert_not_called()
        post.assert_not_called()
        assert path.read_text(encoding="utf-8") == "{invalid"
        entry.async_cancel_retry_setup()


@pytest.mark.parametrize(
    "journal",
    [
        '{"version":true,"claims":{}}',
        '{"version":1,"claims":[]}',
        '{"version":1,"claims":{"bad":{"owner":"x","losers":[]}}}',
        '{"version":1,"claims":{"device_' + "a" * 64 + '":{"owner":"","losers":[]}}}',
        '{"version":1,"claims":{"device_'
        + "a" * 64
        + '":{"owner":"x","losers":["y","y"]}}}',
        '{"version":1,"claims":{"device_'
        + "a" * 64
        + '":{"owner":"x","losers":["x"]}}}',
        '{"version":1,"claims":{"device_'
        + "a" * 64
        + '":{"owner":"x","losers":[]},"device_'
        + "a" * 64
        + '":{"owner":"y","losers":[]}}}',
    ],
)
async def test_invalid_ownership_schema_blocks_persisted_pin(hass, journal):
    from pathlib import Path

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    path = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
    path.write_text(journal)
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth, patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert path.read_text() == journal
        auth.assert_not_called()
        post.assert_not_called()
        entry.async_cancel_retry_setup()


async def test_deleted_claim_fences_loaded_owner_before_post(hass):
    from pathlib import Path
    from unittest.mock import Mock

    from homeassistant.exceptions import HomeAssistantError

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    client.session = Mock()
    client.get_access_token = AsyncMock(return_value="token")
    Path(hass.config.path(".storage", f"{DOMAIN}.claims")).unlink()
    with pytest.raises(HomeAssistantError, match="ownership"):
        await client.set_status(Status.ON)
    client.session.post.assert_not_called()
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"device_binding_{entry.entry_id}"
    )
    await client.async_shutdown()


async def test_mismatched_persisted_claim_blocks_pin(hass):
    import json
    from pathlib import Path

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    path = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "claims": {
                    device_unique_id("a"): {"owner": "other-entry", "losers": []}
                },
            }
        )
    )
    with patch.object(APIClient, "authenticate", AsyncMock()) as auth, patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"device_binding_{entry.entry_id}"
        )
        auth.assert_not_called()
        post.assert_not_called()
        entry.async_cancel_retry_setup()


async def test_unloaded_client_is_fenced_before_post(hass):
    from unittest.mock import Mock

    from homeassistant.exceptions import HomeAssistantError

    from tests.identity_helpers import bound_entry

    entry = bound_entry(domain=DOMAIN, data=DATA, dsn="a")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        client = entry.runtime_data
        client.get_access_token = AsyncMock(return_value="token")
        client.session = Mock()
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(HomeAssistantError, match="reload"):
            await client.set_status(Status.ON)
        client.session.post.assert_not_called()


async def test_missing_claims_require_independent_confirmation(hass):
    from pathlib import Path

    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    from tests.identity_helpers import bound_entry

    first = bound_entry(domain=DOMAIN, title="First", data=DATA, dsn="a")
    second = MockConfigEntry(domain=DOMAIN, title="Second", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=second.entry_id, identifiers={(DOMAIN, "b")}, name="Second"
    )
    Path(hass.config.path(".storage", f"{DOMAIN}.claims")).unlink()
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ) as auth, patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a", "b"])
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post, patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        assert not await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        if second.state is ConfigEntryState.NOT_LOADED:
            assert not await hass.config_entries.async_setup(second.entry_id)
        assert second.state is ConfigEntryState.SETUP_RETRY
        auth.assert_not_called()
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)

        async def confirm(entry, dsn):
            flow = await manager.async_init(
                DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
            )
            flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: dsn})
            flow = await manager.async_configure(
                flow["flow_id"], {"owner": entry.entry_id}
            )
            flow = await manager.async_configure(flow["flow_id"], {})
            assert flow["type"] == "create_entry"
            await hass.async_block_till_done(wait_background_tasks=True)

        await confirm(first, "a")
        assert first.state is ConfigEntryState.LOADED
        assert not await hass.config_entries.async_reload(second.entry_id)
        assert second.state is ConfigEntryState.SETUP_RETRY
        assert CONF_DSN not in second.data
        await confirm(second, "b")
        assert second.state is ConfigEntryState.LOADED
        assert second.data[CONF_DSN] == "b"
        post.assert_not_called()
        await first.runtime_data.async_shutdown()
        await second.runtime_data.async_shutdown()


async def test_repair_journal_write_failure_changes_no_entry(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])):
        await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(flow["flow_id"], {"owner": entry.entry_id})
        with patch(
            "custom_components.delonghi_dehumidifier_api.identity.save_json",
            side_effect=OSError("disk unavailable"),
        ):
            flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["errors"]["base"] == "ownership_unavailable"
        assert entry.data == DATA
        assert entry.unique_id is None
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"device_binding_{entry.entry_id}"
        )
        manager.async_abort(flow["flow_id"])
        entry.async_cancel_retry_setup()


async def test_cancelled_repair_waits_for_write_and_does_not_bind(hass):
    import asyncio
    import threading

    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    from custom_components.delonghi_dehumidifier_api import identity

    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    started = threading.Event()
    release = threading.Event()
    original_write = identity._write_journal

    def delayed_write(path, claims):
        started.set()
        assert release.wait(10)
        original_write(path, claims)

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])):
        await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(flow["flow_id"], {"owner": entry.entry_id})
        with patch.object(identity, "_write_journal", new=delayed_write):
            task = asyncio.create_task(manager.async_configure(flow["flow_id"], {}))
            assert await hass.async_add_executor_job(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert_legacy_entry_unchanged(entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        entry.async_cancel_retry_setup()


async def test_aborted_repair_during_write_keeps_entries_unbound(hass):
    """An aborted HA flow cannot apply an election after its journal write."""
    import asyncio
    import threading

    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.data_entry_flow import UnknownFlow
    from homeassistant.setup import async_setup_component

    from custom_components.delonghi_dehumidifier_api import identity

    owner = MockConfigEntry(domain=DOMAIN, title="Owner", data=DATA, version=1)
    other = MockConfigEntry(domain=DOMAIN, title="Other", data=DATA, version=1)
    owner.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    for entry in (owner, other):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "a")}
        )

    started, release = threading.Event(), threading.Event()
    original_write = identity._write_journal

    def delayed_write(path, claims):
        started.set()
        assert release.wait(10)
        original_write(path, claims)

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ) as auth, patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a"])
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post, patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        assert not await hass.config_entries.async_setup(owner.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{owner.entry_id}"}
        )
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(flow["flow_id"], {"owner": owner.entry_id})
        with patch.object(identity, "_write_journal", new=delayed_write):
            task = asyncio.create_task(manager.async_configure(flow["flow_id"], {}))
            try:
                assert await hass.async_add_executor_job(started.wait, 5)
                manager.async_abort(flow["flow_id"])
                assert not task.done()
            finally:
                release.set()
            try:
                result = await asyncio.wait_for(task, 5)
            except UnknownFlow:
                # Older HA raises when the removed flow finishes its step.
                pass
            else:
                assert result["type"] == "abort"
                assert result["reason"] == "settings_changed"

        for entry in (owner, other):
            assert_legacy_entry_unchanged(entry)
        assert owner.state is ConfigEntryState.SETUP_RETRY
        async with identity.binding_lock(hass):
            journal = await identity.reload_journal(hass)
        assert journal[device_unique_id("a")] == {
            "owner": owner.entry_id,
            "losers": [other.entry_id],
        }
        auth.reset_mock()
        assert not await hass.config_entries.async_reload(owner.entry_id)
        assert owner.state is ConfigEntryState.SETUP_RETRY
        assert_legacy_entry_unchanged(owner)
        assert ir.async_get(hass).async_get_issue(
            DOMAIN, f"device_binding_{owner.entry_id}"
        )
        auth.assert_not_called()
        post.assert_not_called()
        forward.assert_not_called()
        owner.async_cancel_retry_setup()
        if other.state is ConfigEntryState.SETUP_RETRY:
            other.async_cancel_retry_setup()


async def test_transfer_waits_for_old_owner_command_and_fences_old_client(hass):
    import asyncio
    from unittest.mock import Mock

    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.setup import async_setup_component

    from custom_components.delonghi_dehumidifier_api.identity import (
        binding_lock,
        save_election,
    )
    from tests.identity_helpers import bound_entry

    first = bound_entry(domain=DOMAIN, title="First", data=DATA, dsn="a")
    second = MockConfigEntry(domain=DOMAIN, title="Second", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    for entry in (first, second):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "a")}
        )
    async with binding_lock(hass):
        await save_election(hass, "a", first.entry_id, {second.entry_id})
    started, release = asyncio.Event(), asyncio.Event()

    class Response:
        status = 200

        async def __aenter__(self):
            started.set()
            await release.wait()
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {}

    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert second.state is ConfigEntryState.SETUP_RETRY
        old_client = first.runtime_data
        old_client.get_access_token = AsyncMock(return_value="token")
        old_client.session = Mock()
        old_client.session.post.return_value = Response()
        command = asyncio.create_task(old_client.set_status(Status.ON))
        await asyncio.wait_for(started.wait(), 5)
        assert await hass.config_entries.async_remove(first.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)

        async def confirm():
            flow = await manager.async_init(
                DOMAIN, data={"issue_id": f"device_binding_{second.entry_id}"}
            )
            flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
            flow = await manager.async_configure(
                flow["flow_id"], {"owner": second.entry_id}
            )
            return await manager.async_configure(flow["flow_id"], {})

        try:
            result = await confirm()
            assert result["type"] == "abort"
            assert result["reason"] == "already_bound"
        finally:
            release.set()
        await command
        result = await confirm()
        assert result["type"] == "create_entry"
        await hass.async_block_till_done(wait_background_tasks=True)
        assert second.state is ConfigEntryState.LOADED
        with pytest.raises(HomeAssistantError, match="reload"):
            await old_client.set_status(Status.ON)
        assert old_client.session.post.call_count == 1
        await second.runtime_data.async_shutdown()


async def test_disabled_pinned_owner_is_not_silently_replaced(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.config_entries import ConfigEntryDisabler
    from homeassistant.setup import async_setup_component

    from tests.identity_helpers import bound_entry

    first = bound_entry(
        domain=DOMAIN,
        title="Disabled owner",
        data=DATA,
        dsn="a",
        disabled_by=ConfigEntryDisabler.USER,
    )
    second = MockConfigEntry(domain=DOMAIN, title="Other", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    for entry in (first, second):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, "a")}
        )
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["a"])
    ), patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert not await hass.config_entries.async_setup(second.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{second.entry_id}"}
        )
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(
            flow["flow_id"], {"owner": second.entry_id}
        )
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["type"] == "abort"
        assert flow["reason"] == "already_bound"
        assert CONF_DSN not in second.data
        assert first.data[CONF_DSN] == "a"
        post.assert_not_called()
        second.async_cancel_retry_setup()


async def test_removed_owner_does_not_promote_loser_without_explicit_reelection(hass):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.setup import async_setup_component

    from custom_components.delonghi_dehumidifier_api.identity import (
        binding_lock,
        save_election,
    )
    from tests.identity_helpers import bound_entry

    first = bound_entry(domain=DOMAIN, title="First", data=DATA, dsn="a")
    second = MockConfigEntry(domain=DOMAIN, title="Second", data=DATA, version=1)
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=first.entry_id, identifiers={(DOMAIN, "a")}
    )
    registry.async_get_or_create(
        config_entry_id=second.entry_id, identifiers={(DOMAIN, "a")}
    )
    async with binding_lock(hass):
        await save_election(hass, "a", first.entry_id, {second.entry_id})
    with patch.object(
        APIClient, "authenticate", AsyncMock(return_value=True)
    ), patch.object(APIClient, "get_devices", AsyncMock(return_value=["a"])), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        assert second.state is ConfigEntryState.SETUP_RETRY
        assert CONF_DSN not in second.data
        await hass.config_entries.async_remove(first.entry_id)
        assert dr.async_entries_for_config_entry(registry, second.entry_id)
        assert not await hass.config_entries.async_reload(second.entry_id)
        assert second.state is ConfigEntryState.SETUP_RETRY
        assert CONF_DSN not in second.data
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        flow = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{second.entry_id}"}
        )
        flow = await manager.async_configure(flow["flow_id"], {CONF_DSN: "a"})
        flow = await manager.async_configure(
            flow["flow_id"], {"owner": second.entry_id}
        )
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["type"] == "create_entry"
        await hass.async_block_till_done()
        assert second.state is ConfigEntryState.LOADED
        assert second.data[CONF_DSN] == "a"
        assert dr.async_entries_for_config_entry(registry, second.entry_id)
        await second.runtime_data.async_shutdown()
