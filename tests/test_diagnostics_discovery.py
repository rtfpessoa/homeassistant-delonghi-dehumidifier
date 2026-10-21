"""Verify Home Assistant discovers the integration's diagnostics handler."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN


@pytest.mark.parametrize("frontend_first", [True, False])
async def test_frontend_discovers_diagnostics(hass, hass_ws_client, frontend_first):
    """The admin API advertises entry diagnostics after normal frontend startup."""
    properties = {
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 1,
    }
    records = [
        {"property": {"name": name, "value": value, "product_name": "Test"}}
        for name, value in properties.items()
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="TEST-DSN")),
        patch.object(APIClient, "get_request", AsyncMock(return_value=records)),
    ):
        if frontend_first:
            assert await async_setup_component(hass, "frontend", {})
        assert await hass.config_entries.async_setup(entry.entry_id)
        if not frontend_first:
            assert await async_setup_component(hass, "frontend", {})
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert "diagnostics" in hass.config.components
        client = await hass_ws_client()
        await client.send_json_auto_id({"type": "diagnostics/get", "domain": DOMAIN})
        response = await client.receive_json()
        assert response["success"] is True
        assert response["result"] == {
            "domain": DOMAIN,
            "handlers": {"config_entry": True, "device": False},
        }

        assert await hass.config_entries.async_unload(entry.entry_id)
