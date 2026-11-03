"""Test the options flow and config-entry reload lifecycle."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry as LegacyConfigEntry,
)

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import (
    CONF_DSN,
    require_restart,
)
from tests.identity_helpers import bound_entry as MockConfigEntry

ORIGINAL = {
    CONF_EMAIL: "old@example.com",
    CONF_PASSWORD: "old-password",
    CONF_LANGUAGE: "en",
}
UPDATED = {
    CONF_EMAIL: "new@example.com",
    CONF_PASSWORD: "new-password",
    CONF_LANGUAGE: "pt",
}


@pytest.fixture
def mock_client():
    values = {
        "appliance_model": "DDSX220WFA",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "filter_status": 1,
        "heat_exchanger_temp": 220,
        "room_temp": 210,
        "swing": 0,
        "set_eco": 0,
    }
    properties = [{"product_name": "Test"}] + [
        {"name": name, "value": value} for name, value in values.items()
    ]
    with patch.multiple(
        APIClient,
        authenticate=AsyncMock(return_value=True),
        get_first_device=AsyncMock(return_value="test-dsn"),
        get_devices=AsyncMock(return_value=["test-dsn"]),
        get_properties=AsyncMock(return_value=properties),
    ):
        yield


def client_settings(entry):
    """Return the active client's configured values."""
    client = entry.runtime_data
    return {
        CONF_EMAIL: client.email,
        CONF_PASSWORD: client.password,
        CONF_LANGUAGE: client.language,
    }


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ORIGINAL),
        (UPDATED, UPDATED),
        ({CONF_PASSWORD: "new-password"}, {**ORIGINAL, CONF_PASSWORD: "new-password"}),
    ],
)
async def test_setup_preserves_and_uses_legacy_options(
    hass, mock_client, options, expected
):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert client_settings(entry) == expected
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        result["data_schema"]({})
    assert result["data_schema"]({CONF_PASSWORD: "entered-password"}) == {
        **expected,
        CONF_PASSWORD: "entered-password",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_successful_options_reload_and_unload(hass, mock_client):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original_client = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=UPDATED
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.options == {}
    assert entry.data == {**UPDATED, "device_dsn": "test-dsn"}
    assert entry.runtime_data is not original_client
    assert client_settings(entry) == UPDATED
    assert len(entry.update_listeners) == 1

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({CONF_PASSWORD: "entered-password"}) == {
        **UPDATED,
        CONF_PASSWORD: "entered-password",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert len(entry.update_listeners) == 0


async def test_invalid_auth_does_not_save_or_reload(hass, mock_client):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL, options=UPDATED)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original_client = entry.runtime_data
    APIClient.authenticate.return_value = False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    invalid = {**UPDATED, CONF_PASSWORD: "invalid-password"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=invalid
    )
    await hass.async_block_till_done()

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"
    assert entry.options == UPDATED
    assert entry.data == {**ORIGINAL, "device_dsn": "test-dsn"}
    assert entry.runtime_data is original_client
    assert client_settings(entry) == UPDATED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def open_account_form(hass, entry, kind):
    """Open the public UI path for changing connection settings."""
    if kind == "options":
        manager = hass.config_entries.options
        return manager, await manager.async_init(entry.entry_id)
    manager = hass.config_entries.flow
    return manager, await manager.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
async def test_existing_account_form_uses_current_settings(hass, mock_client, kind):
    options = {CONF_EMAIL: "previous@example.com"}
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL, options=options)
    entry.add_to_hass(hass)
    manager, form = await open_account_form(hass, entry, kind)
    with pytest.raises(vol.Invalid):
        form["data_schema"]({})
    assert form["data_schema"]({CONF_PASSWORD: "entered"}) == {
        CONF_EMAIL: options[CONF_EMAIL],
        CONF_LANGUAGE: ORIGINAL[CONF_LANGUAGE],
        CONF_PASSWORD: "entered",
    }
    category = "options" if kind == "options" else "config"
    for locale in ("en", "de"):
        messages = await async_get_translations(hass, locale, category, [DOMAIN])
        for field in (CONF_LANGUAGE, CONF_EMAIL, CONF_PASSWORD):
            assert messages[
                f"component.{DOMAIN}.{category}.step.{form['step_id']}.data_description.{field}"
            ]
    manager.async_abort(form["flow_id"])


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
@pytest.mark.parametrize(
    ("method", "failure", "error"),
    [
        ("authenticate", InvalidCredentialsError(), "invalid_auth"),
        ("get_devices", TransientCloudError(), "cannot_connect"),
        ("get_devices", NoDevicesError(), "no_devices"),
        ("get_devices", ["another-dsn"], "device_not_available"),
        ("get_devices", ValueError("private response"), "unknown"),
    ],
)
async def test_account_settings_recovery(
    hass, mock_client, kind, method, failure, error
):
    """Recover on the same form without changing the bound device or its entities."""
    options = {CONF_EMAIL: "previous@example.com", "unrelated": 7}
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    before = entry.runtime_data
    original_data, unique_id = dict(entry.data), entry.unique_id
    registry = er.async_get(hass)
    entity_ids = {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entity_ids
    manager, form = await open_account_form(hass, entry, kind)
    assert form["type"] == "form"
    rejected = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    with patch.object(APIClient, method, rejected):
        result = await manager.async_configure(form["flow_id"], user_input=UPDATED)
    assert result["type"] == "form"
    assert result["errors"] == {"base": error}
    assert entry.data == original_data and entry.options == options
    assert entry.runtime_data is before
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_entry:
        result = await manager.async_configure(form["flow_id"], user_input=UPDATED)
        await hass.async_block_till_done()
    if kind == "reconfigure":
        assert (
            result["type"] == "abort" and result["reason"] == "reconfigure_successful"
        )
    else:
        assert result["type"] == "create_entry"
    reload_entry.assert_awaited_once_with(entry.entry_id)
    assert entry.data == {**original_data, **UPDATED}
    assert entry.options == {"unrelated": 7}
    assert_account_identity(hass, entry, unique_id, original_data[CONF_DSN], entity_ids)
    assert client_settings(entry) == UPDATED
    assert await hass.config_entries.async_unload(entry.entry_id)


def assert_account_identity(hass, entry, unique_id, dsn, entity_ids):
    """The reload preserves the entry, physical appliance and registered entities."""
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.unique_id == unique_id
    assert entry.runtime_data.device_dsn == dsn
    assert entity_ids == {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
@pytest.mark.parametrize("interrupt", ["change", "cancel", "restart"])
async def test_stale_account_edit_cannot_apply(hass, mock_client, kind, interrupt):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL)
    entry.add_to_hass(hass)
    manager, form = await open_account_form(hass, entry, kind)
    original_data = dict(entry.data)
    started, resume = asyncio.Event(), asyncio.Event()

    async def verify(*args):
        started.set()
        await resume.wait()

    with (
        patch.object(APIClient, "verify_device", verify),
        patch.object(hass.config_entries, "async_schedule_reload") as reload_entry,
    ):
        pending = asyncio.create_task(
            manager.async_configure(form["flow_id"], user_input=UPDATED)
        )
        await asyncio.wait_for(started.wait(), 5)
        if interrupt == "change":
            hass.config_entries.async_update_entry(entry, options={"newer": True})
        elif interrupt == "cancel":
            manager.async_abort(form["flow_id"])
        else:
            require_restart(hass, entry.entry_id, entry.data[CONF_DSN])
        resume.set()
        try:
            result = await pending
        except UnknownFlow:
            assert interrupt == "cancel"
        else:
            assert result["type"] == "abort"
            assert result["reason"] == (
                "restart_required" if interrupt == "restart" else "settings_changed"
            )
        reload_entry.assert_not_called()
    assert entry.data == original_data
    assert entry.options == ({"newer": True} if interrupt == "change" else {})


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
async def test_legacy_account_edit_requires_device_confirmation(
    hass, mock_client, kind
):
    entry = LegacyConfigEntry(domain=DOMAIN, data=ORIGINAL)
    entry.add_to_hass(hass)
    _, result = await open_account_form(hass, entry, kind)
    assert result["type"] == "abort"
    assert result["reason"] == "device_binding_required"
    APIClient.authenticate.assert_not_awaited()
    assert entry.data == ORIGINAL


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("stored", ["missing", "corrupt", "other_owner"])
async def test_edit_rejects_lost_ownership(hass, mock_client, kind, stored, loaded):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL)
    entry.add_to_hass(hass)
    if loaded:
        assert await hass.config_entries.async_setup(entry.entry_id)
    manager, form = await open_account_form(hass, entry, kind)
    APIClient.authenticate.reset_mock()
    journal = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
    if stored == "missing":
        journal.unlink()
    elif stored == "corrupt":
        journal.write_text("invalid json")
    else:
        record = json.loads(journal.read_text())
        record["claims"][entry.unique_id]["owner"] = "another-entry"
        journal.write_text(json.dumps(record))
    result = await manager.async_configure(form["flow_id"], user_input=UPDATED)
    assert result["type"] == "abort" and result["reason"] == "device_binding_required"
    APIClient.authenticate.assert_not_awaited()
    assert entry.data == {**ORIGINAL, CONF_DSN: "test-dsn"}
    if loaded:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("kind", ["reconfigure", "options"])
async def test_unchanged_edit_reloads_once(hass, mock_client, kind):
    entry = MockConfigEntry(domain=DOMAIN, data=ORIGINAL)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    manager, form = await open_account_form(hass, entry, kind)
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_entry:
        await manager.async_configure(form["flow_id"], user_input=ORIGINAL)
        await hass.async_block_till_done()
    reload_entry.assert_awaited_once_with(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
