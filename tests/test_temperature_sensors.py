"""Test temperature readings from the dehumidifier."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    UnitOfTemperature,
)
from homeassistant.helpers import entity_registry as er

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.sensor import get_temperature_celsius
from tests.identity_helpers import bound_entry as MockConfigEntry


async def test_missing_temperature_is_not_converted():
    """A nullable client result remains unknown instead of becoming zero."""
    assert await get_temperature_celsius(AsyncMock(return_value=None))() is None


@pytest.mark.parametrize(
    ("room_raw", "exchanger_raw", "room_expected", "exchanger_expected"),
    [
        (210, 220, 21.0, 22.0),
        (0, 0, 0.0, 0.0),
        (-105, -15, -10.5, -1.5),
        (None, None, None, None),
        (None, "210", None, 21.0),
    ],
)
async def test_temperature_sensors_use_tenths_of_celsius(
    hass, room_raw, exchanger_raw, room_expected, exchanger_expected
):
    """Publish the device temperatures as Celsius, including missing values."""
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
        "heat_exchanger_temp": exchanger_raw,
        "room_temp": room_raw,
        "swing": 0,
        "set_eco": 0,
    }
    properties = [{"product_name": "Test"}] + [
        {"name": name, "value": value} for name, value in values.items()
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
    client_mocks = {
        "authenticate": AsyncMock(return_value=True),
        "get_first_device": AsyncMock(return_value="test-dsn"),
        "get_devices": AsyncMock(return_value=["test-dsn"]),
        "get_properties": AsyncMock(return_value=properties),
    }
    # Null values reach the wrapper once the separate client null fix is applied.
    if room_raw is None:
        client_mocks["get_room_temp"] = AsyncMock(return_value=None)
    if exchanger_raw is None:
        client_mocks["get_heat_exchanger_temp"] = AsyncMock(return_value=None)

    with patch.multiple(APIClient, **client_mocks):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        for name, expected in (
            ("room_temperature", room_expected),
            ("heat_exchanger_temperature", exchanger_expected),
        ):
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_test-dsn_{name}_sensor"
            )
            assert entity_id is not None
            state = hass.states.get(entity_id)
            assert state is not None
            assert state.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS
            if expected is None:
                assert state.state == "unknown"
            else:
                assert float(state.state) == expected

            # Home Assistant converts the native Celsius value when an entity
            # is configured to display Fahrenheit, including existing entities.
            registry.async_update_entity_options(
                entity_id,
                "sensor",
                {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
            )
            await hass.async_block_till_done()
            state = hass.states.get(entity_id)
            assert (
                state.attributes["unit_of_measurement"] == UnitOfTemperature.FAHRENHEIT
            )
            if expected is None:
                assert state.state == "unknown"
            else:
                assert float(state.state) == pytest.approx(expected * 9 / 5 + 32)

        assert await hass.config_entries.async_unload(entry.entry_id)
