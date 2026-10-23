"""Exercise the integration lifecycle with a mocked cloud device."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests.identity_helpers import bound_entry as MockConfigEntry


async def test_setup_and_unload_device(hass):
    """All platforms create real entities and unload them with the entry."""
    properties = {
        "product_name": "Test",
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 54,
        "humidity_setpoint": 60,
        "current_speed": 1,
        "filter_status": 1,
        "room_temp": 70,
        "heat_exchanger_temp": 70,
        "device_mode": 1,
        "device_status": 1,
        "set_eco": 1,
        "swing": 0,
        "filter_change_alarm": 0,
        "filter_life": 100,
    }
    device_properties = [
        {"property": {"name": name, "value": value, "product_name": "Test"}}
        for name, value in properties.items()
    ]
    entry = MockConfigEntry(
        dsn="TEST-DSN",
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test-password",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="TEST-DSN")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["TEST-DSN"])),
        patch.object(
            APIClient, "get_request", AsyncMock(return_value=device_properties)
        ) as get_request,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        mode_entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_device_mode_sensor"
        )
        assert mode_entity_id == "sensor.test_dehumidifier_cloud_mode_raw"
        expected_states = {
            "humidifier.test_dehumidifier_unit": "on",
            "sensor.test_dehumidifier_current_humidity": "54",
            "switch.test_dehumidifier_eco_mode": "on",
            "switch.test_dehumidifier_swing_mode": "off",
            mode_entity_id: "DEHUMIDIFY",
        }
        entity_ids = set(expected_states) | {
            "sensor.test_dehumidifier_target_humidity",
            "sensor.test_dehumidifier_current_speed_raw",
            "sensor.test_dehumidifier_filter_status",
            "sensor.test_dehumidifier_room_temperature",
            "sensor.test_dehumidifier_heat_exchanger_temperature",
            "sensor.test_dehumidifier_device_status",
            "sensor.test_dehumidifier_eco_mode",
            "sensor.test_dehumidifier_swing_mode",
            "sensor.test_dehumidifier_filter_change_alarm",
            "sensor.test_dehumidifier_filter_life",
        }
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            assert state is not None
            assert state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        for entity_id, expected_state in expected_states.items():
            state = hass.states.get(entity_id)
            assert state is not None
            assert state.state == expected_state
        get_request.assert_awaited_with("apiv1/dsns/TEST-DSN/properties.json")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            assert state is None or state.state == STATE_UNAVAILABLE
