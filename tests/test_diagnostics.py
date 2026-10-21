"""Test safe diagnostic downloads from real integration setup."""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.diagnostics import (
    async_get_config_entry_diagnostics,
)

SECRETS = (
    "diagnostics-private@example.com",
    "diagnostics-private-password",
    "diagnostics-private-token",
    "diagnostics-private-dsn",
    "https://private.example/diagnostics-secret",
    "diagnostics-private-message",
)


@pytest.fixture
async def diagnostics_entry(hass):
    """Set up with realistic API records containing unrelated secret fields."""
    values = {
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
        "alarm_state": 3,
        "set_eco": 0,
        "swing": 1,
    }
    records = [
        {
            "property": {
                "name": name,
                "value": value,
                "product_name": "Test",
                "ack_enabled": True,
                "ack_status": 0,
                "ack_message": SECRETS[5],
                "email": SECRETS[0],
                "password": SECRETS[1],
                "access_token": SECRETS[2],
                "dsn": SECRETS[3],
                "url": SECRETS[4],
                "nested": {"credential": SECRETS[2]},
            }
        }
        for name, value in values.items()
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: SECRETS[0], CONF_PASSWORD: SECRETS[1], CONF_LANGUAGE: "en"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value=SECRETS[3])),
        patch.object(APIClient, "get_request", AsyncMock(return_value=records)) as get,
        patch.object(APIClient, "post_request", new_callable=AsyncMock) as post,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        get.reset_mock()
        yield entry, get, post
        await hass.config_entries.async_unload(entry.entry_id)


async def test_download_only_exports_allowlisted_property_fields(
    hass, hass_client, diagnostics_entry
):
    """Real HA download excludes secrets and does not call the cloud."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.access_token = SECRETS[2]
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200
    download = await response.json()
    exported = json.dumps(download)
    assert all(secret not in exported for secret in SECRETS)
    data = download["data"]
    assert data["status"] == "available"
    assert isinstance(data["cache_age_seconds"], int)
    assert data["cache_age_seconds"] >= 0
    assert data["properties"] == {
        "alarm_state": {
            "present": True,
            "value": 3,
            "ack_enabled": True,
            "ack_status": 0,
        },
        "set_eco": {"present": True, "value": 0, "ack_enabled": True, "ack_status": 0},
        "swing": {"present": True, "value": 1, "ack_enabled": True, "ack_status": 0},
    }
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_missing_and_malformed_properties_do_not_leak(hass, diagnostics_entry):
    """Distinguish absent properties from null values and reject arbitrary fields."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = [
        {
            "name": "alarm_state",
            "value": None,
            "ack_enabled": "true",
            "ack_status": SECRETS[2],
        },
        {
            "name": "swing",
            "value": SECRETS[4],
            "ack_enabled": True,
            "ack_status": 2**63,
        },
        {"name": ["set_eco"], "value": SECRETS[0]},
        {"name": SECRETS[2], "value": SECRETS[1]},
        None,
    ]
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["properties"] == {
        "alarm_state": {
            "present": True,
            "value": None,
            "ack_enabled": None,
            "ack_status": None,
        },
        "set_eco": {
            "present": False,
            "value": None,
            "ack_enabled": None,
            "ack_status": None,
        },
        "swing": {
            "present": True,
            "value": None,
            "ack_enabled": True,
            "ack_status": None,
        },
    }
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_failed_poll_with_no_cache_is_unavailable(hass, diagnostics_entry):
    """A failed poll exposes no exception detail and diagnostics triggers no retry."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = None
    entry.runtime_data.device_properties_timestamp = 0
    get.side_effect = HomeAssistantError(SECRETS[2])
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.get_properties()
    get.reset_mock()
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data == {
        "status": "unavailable",
        "cache_age_seconds": None,
        "properties": {},
    }
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_failed_poll_keeps_age_of_last_successful_snapshot(
    hass, diagnostics_entry
):
    """A failed refresh cannot make old data look fresh or expose error text."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties_timestamp = time.time() - 120
    get.side_effect = HomeAssistantError(SECRETS[2])
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.get_properties()
    get.reset_mock()
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["status"] == "available"
    assert data["cache_age_seconds"] >= 120
    assert data["properties"]["alarm_state"]["value"] == 3
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()
