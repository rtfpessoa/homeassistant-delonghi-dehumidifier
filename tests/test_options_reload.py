"""Test the options flow and config-entry reload lifecycle."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

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

    assert entry.options == UPDATED
    assert entry.data == ORIGINAL
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
    assert entry.data == ORIGINAL
    assert entry.runtime_data is original_client
    assert client_settings(entry) == UPDATED
    assert await hass.config_entries.async_unload(entry.entry_id)
