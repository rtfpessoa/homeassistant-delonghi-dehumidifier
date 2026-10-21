"""Tests for device properties returned without a value."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.delonghi_dehumidifier_api.client import APIClient, OffOnStatus
from custom_components.delonghi_dehumidifier_api.const import DOMAIN


@pytest.fixture
async def device_with_missing_properties(hass):
    """Set up an integration whose device returns null and missing properties."""
    properties = {
        "product_name": "Test",
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 52,
        "humidity_setpoint": None,
        "current_speed": 2,
        "filter_status": None,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "device_mode": None,
        "device_status": None,
        "set_eco": None,
        # swing is missing entirely.
        "filter_change_alarm": 0,
        "filter_life": 100,
    }

    async def get_properties_request(_path):
        return [
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
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(
            APIClient, "get_request", AsyncMock(side_effect=get_properties_request)
        ) as get_request,
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="TEST-DSN")),
        patch.object(APIClient, "set_eco", new_callable=AsyncMock) as set_eco,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, properties, get_request, set_eco
        await hass.config_entries.async_unload(entry.entry_id)


def entity_id(hass, platform, suffix):
    """Find an entity by its unchanged unique ID."""
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{DOMAIN}_TEST-DSN_{suffix}"
    )


async def refresh(hass, entry, entities):
    """Request a fresh cloud value and update the selected entities."""
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entities}, blocking=True
    )


async def test_missing_properties_allow_setup(hass, device_with_missing_properties):
    """Missing values remain unknown without preventing unrelated sensors."""
    assert (
        hass.states.get(entity_id(hass, "sensor", "current_humidity_sensor")).state
        == "52"
    )
    for platform, suffix in (
        ("switch", "eco_mode_switch"),
        ("switch", "swing_mode_switch"),
        ("sensor", "filter_status_sensor"),
        ("sensor", "target_humidity_sensor"),
        ("sensor", "eco_mode_sensor"),
        ("sensor", "swing_mode_sensor"),
        ("humidifier", "dehumidifier"),
    ):
        assert hass.states.get(entity_id(hass, platform, suffix)).state == STATE_UNKNOWN


async def test_known_values_recover_after_refresh(hass, device_with_missing_properties):
    """Sensors and the humidifier recover when the API supplies values."""
    entry, properties, _, _ = device_with_missing_properties
    properties.update(
        {
            "set_eco": 1,
            "swing": 0,
            "filter_status": 1,
            "device_mode": 1,
            "device_status": 1,
            "humidity_setpoint": 60,
        }
    )
    expected = {
        entity_id(hass, "sensor", "filter_status_sensor"): "GOOD",
        entity_id(hass, "sensor", "target_humidity_sensor"): "60",
        entity_id(hass, "sensor", "eco_mode_sensor"): "ON",
        entity_id(hass, "sensor", "swing_mode_sensor"): "OFF",
        entity_id(hass, "humidifier", "dehumidifier"): "on",
    }
    await refresh(hass, entry, list(expected))
    for entity, state in expected.items():
        assert hass.states.get(entity).state == state
    assert (
        hass.states.get(entity_id(hass, "humidifier", "dehumidifier")).attributes[
            "humidity"
        ]
        == 60
    )


async def test_unknown_enum_values_remain_unknown(hass, device_with_missing_properties):
    """Unrecognized numeric enum values are not interpreted as known states."""
    entry, properties, _, _ = device_with_missing_properties
    properties.update(
        {
            "set_eco": 9,
            "swing": None,
            "filter_status": 0,
            "device_mode": 99,
            "device_status": 99,
        }
    )
    entities = [
        entity_id(hass, "sensor", "filter_status_sensor"),
        entity_id(hass, "sensor", "eco_mode_sensor"),
        entity_id(hass, "sensor", "swing_mode_sensor"),
        entity_id(hass, "humidifier", "dehumidifier"),
    ]
    await refresh(hass, entry, entities)
    for entity in entities:
        assert hass.states.get(entity).state == STATE_UNKNOWN


async def test_switch_command_is_not_overwritten_by_stale_cache(
    hass, device_with_missing_properties
):
    """Existing switch behavior keeps the commanded state until proper polling exists."""
    _, _, get_request, set_eco = device_with_missing_properties
    eco = entity_id(hass, "switch", "eco_mode_switch")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": eco}, blocking=True
    )
    set_eco.assert_awaited_once_with(OffOnStatus.ON)
    assert hass.states.get(eco).state == "on"
    assert get_request.await_count == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert get_request.await_count == 1
    assert hass.states.get(eco).state == "on"
