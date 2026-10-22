"""Shared simulated cloud fixtures for Real Feel tests."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
)


@pytest.fixture
async def cloud(hass, request):
    """Load a mutable simulated device, without contacting the cloud."""
    values = {
        "product_name": "Test",
        "appliance_model": "DDSX220",
        "firmware_version": "1.0",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "activate_realfeel": REAL_FEEL_ACTIVATION_PAYLOAD,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
    }
    values.update(getattr(request, "param", {}))

    def snapshot():
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in values.items()
        ]

    async def read(_path):
        return snapshot()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.invalid",
            CONF_PASSWORD: "test-password",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=read)) as get,
        patch.object(APIClient, "post_request", AsyncMock(return_value={})) as post,
    ):
        yield entry, values, snapshot, get, post
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def setup(hass, cloud):
    entry, *_ = cloud
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
