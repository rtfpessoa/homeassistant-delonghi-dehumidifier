"""Test the filter-life sensor's native unit and duration."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, UnitOfTime
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN


@pytest.mark.parametrize(
    ("raw_minutes", "expected_days"),
    [(458960, 458960 / 1440), (0, 0.0), (None, None)],
)
async def test_filter_life_minutes_are_reported_as_days(
    hass, raw_minutes, expected_days
):
    """Publish the API duration in days and retain a missing client value."""
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
        "filter_life": raw_minutes,
        "filter_status": 1,
        "heat_exchanger_temp": 220,
        "room_temp": 210,
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
        "get_first_device": AsyncMock(return_value="test-dsn"),
        "get_properties": AsyncMock(return_value=properties),
    }
    # Actual API null handling belongs to a separate change.
    if raw_minutes is None:
        client_mocks["get_filter_life"] = AsyncMock(return_value=None)

    with patch.multiple(APIClient, **client_mocks):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_test-dsn_filter_life_sensor"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        if expected_days is None:
            assert state.state == "unknown"
        else:
            assert float(state.state) == pytest.approx(expected_days)
        assert state.attributes["unit_of_measurement"] == UnitOfTime.DAYS

        # Verify that Home Assistant can still display the sensor in hours.
        registry.async_update_entity_options(
            entity_id, "sensor", {"unit_of_measurement": UnitOfTime.HOURS}
        )
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.attributes["unit_of_measurement"] == UnitOfTime.HOURS
        if expected_days is None:
            assert state.state == "unknown"
        else:
            assert float(state.state) == pytest.approx(raw_minutes / 60)

        assert await hass.config_entries.async_unload(entry.entry_id)
