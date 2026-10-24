"""Test safe diagnostic downloads from real integration setup."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
)
from custom_components.delonghi_dehumidifier_api.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.identity_helpers import bound_entry as MockConfigEntry

SECRETS = (
    "diagnostics-private@example.com",
    "diagnostics-private-password",
    "diagnostics-private-token",
    "diagnostics-private-dsn",
    "https://private.example/diagnostics-secret",
    "diagnostics-private-message",
)
CANDIDATE_DEACTIVATION_PAYLOAD = "AAIDChIXHEY8Mig="


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
        "activate_realfeel": REAL_FEEL_ACTIVATION_PAYLOAD,
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
        dsn=SECRETS[3],
        domain=DOMAIN,
        data={CONF_EMAIL: SECRETS[0], CONF_PASSWORD: SECRETS[1], CONF_LANGUAGE: "en"},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value=SECRETS[3])),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=[SECRETS[3]])),
        patch.object(APIClient, "get_request", AsyncMock(return_value=records)) as get,
        patch.object(APIClient, "post_request", new_callable=AsyncMock) as post,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        get.reset_mock()
        yield entry, get, post
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("raw_value", "activation_match", "candidate_match"),
    [
        (REAL_FEEL_ACTIVATION_PAYLOAD, True, False),
        (CANDIDATE_DEACTIVATION_PAYLOAD, False, True),
        (SECRETS[2], False, False),
    ],
)
async def test_download_only_exports_allowlisted_property_fields(
    hass, hass_client, diagnostics_entry, raw_value, activation_match, candidate_match
):
    """Real HA download excludes secrets and does not call the cloud."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.access_token = SECRETS[2]
    for prop in entry.runtime_data.device_properties:
        if prop.get("name") == "activate_realfeel":
            prop["value"] = raw_value
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == 200
    download = await response.json()
    exported = json.dumps(download)
    assert all(secret not in exported for secret in SECRETS)
    assert REAL_FEEL_ACTIVATION_PAYLOAD not in exported
    assert CANDIDATE_DEACTIVATION_PAYLOAD not in exported
    data = download["data"]
    assert data["status"] == "available"
    assert isinstance(data["cache_age_seconds"], int)
    assert data["cache_age_seconds"] >= 0
    assert isinstance(data["request_age_seconds"], int)
    assert data["request_age_seconds"] >= 0
    assert data["properties"] == {
        "alarm_state": {
            "present": True,
            "value": 3,
            "ack_enabled": True,
            "ack_status": 0,
        },
        "set_eco": {"present": True, "value": 0, "ack_enabled": True, "ack_status": 0},
        "swing": {"present": True, "value": 1, "ack_enabled": True, "ack_status": 0},
        "device_mode": {
            "present": True,
            "value": 1,
            "ack_enabled": True,
            "ack_status": 0,
            "value_out_of_range": False,
        },
        "activate_realfeel": {
            "present": True,
            "value": None,
            "ack_enabled": True,
            "ack_status": 0,
            "matches_activation_command": activation_match,
            "matches_candidate_deactivation_command": candidate_match,
        },
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
        "device_mode": {
            "present": False,
            "value": None,
            "ack_enabled": None,
            "ack_status": None,
            "value_out_of_range": None,
        },
        "activate_realfeel": {
            "present": False,
            "value": None,
            "ack_enabled": None,
            "ack_status": None,
            "matches_activation_command": None,
            "matches_candidate_deactivation_command": None,
        },
    }
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "expected", "out_of_range"),
    [
        (0, 0, False),
        (42, 42, False),
        (100, 100, False),
        (255, 255, False),
        (-1, None, True),
        (256, None, True),
        (2**31 - 1, None, True),
        (2**31, None, True),
        (True, None, None),
        (1.0, None, None),
        (SECRETS[2], None, None),
        (None, None, None),
    ],
)
async def test_device_mode_diagnostics_limits_values(
    hass, diagnostics_entry, value, expected, out_of_range
):
    """Mode diagnostics accept only a byte-sized integer, not arbitrary values."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = [
        {
            "name": "device_mode",
            "value": value,
            "ack_enabled": SECRETS[0],
            "ack_status": SECRETS[1],
            "private": SECRETS[3],
        }
    ]
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["properties"]["device_mode"] == {
        "present": True,
        "value": expected,
        "ack_enabled": None,
        "ack_status": None,
        "value_out_of_range": out_of_range,
    }
    if type(value) is int and out_of_range:
        assert str(value) not in json.dumps(data)
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.parametrize("name", ["device_mode", "activate_realfeel"])
@pytest.mark.parametrize(
    ("ack_status", "expected"),
    [
        (0, 0),
        (255, 255),
        (-1, None),
        (256, None),
        (2**31 - 1, None),
        (True, None),
        (SECRETS[2], None),
    ],
)
async def test_mode_acknowledgement_status_is_byte_sized(
    hass, diagnostics_entry, name, ack_status, expected
):
    """New mode properties do not reveal arbitrary acknowledgement numbers."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = [
        {"name": name, "value": None, "ack_status": ack_status}
    ]
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["properties"][name]["ack_status"] == expected
    if type(ack_status) is int and not 0 <= ack_status <= 255:
        assert str(ack_status) not in json.dumps(data)
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_existing_properties_keep_original_integer_bounds(
    hass, diagnostics_entry
):
    """Privacy bounds for new properties do not change existing diagnostics."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = [
        {"name": "alarm_state", "value": -1, "ack_status": 2**31 - 1}
    ]
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["properties"]["alarm_state"]["value"] == -1
    assert data["properties"]["alarm_state"]["ack_status"] == 2**31 - 1
    get.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "activation_match", "candidate_match"),
    [
        (REAL_FEEL_ACTIVATION_PAYLOAD, True, False),
        (CANDIDATE_DEACTIVATION_PAYLOAD, False, True),
        (REAL_FEEL_ACTIVATION_PAYLOAD + "x", False, False),
        (CANDIDATE_DEACTIVATION_PAYLOAD + "x", False, False),
        ("", False, False),
        (SECRETS[2], False, False),
        (0, None, None),
        (2**31 - 1, None, None),
        (True, None, None),
        (None, None, None),
        ({"secret": SECRETS[2]}, None, None),
        ([REAL_FEEL_ACTIVATION_PAYLOAD], None, None),
    ],
)
async def test_real_feel_command_comparison_is_exact_and_does_not_export_strings(
    hass, diagnostics_entry, value, activation_match, candidate_match
):
    """Compare opaque values without exposing them or calling the cloud."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties = [
        {"name": "activate_realfeel", "value": value}
    ]
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert (
        data["properties"]["activate_realfeel"]["matches_activation_command"]
        is activation_match
    )
    assert (
        data["properties"]["activate_realfeel"][
            "matches_candidate_deactivation_command"
        ]
        is candidate_match
    )
    assert data["properties"]["activate_realfeel"]["value"] is None
    exported = json.dumps(data)
    assert REAL_FEEL_ACTIVATION_PAYLOAD not in exported
    assert CANDIDATE_DEACTIVATION_PAYLOAD not in exported
    if type(value) is str and value:
        assert value not in exported
    if type(value) is int and value > 255:
        assert str(value) not in exported
    assert all(secret not in exported for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_request_age_detects_poll_started_before_a_mode_change(
    hass, diagnostics_entry
):
    """A recently completed GET can still contain an older mode snapshot."""
    entry, get, post = diagnostics_entry
    started = asyncio.Event()
    finish = asyncio.Event()
    current_mode = 1

    async def delayed_get(_url):
        snapshot = current_mode
        started.set()
        await finish.wait()
        return [{"property": {"name": "device_mode", "value": snapshot}}]

    get.side_effect = delayed_get
    entry.runtime_data.device_properties_timestamp = 0
    with patch("custom_components.delonghi_dehumidifier_api.client.time") as clock:
        clock.time.side_effect = time.time
        clock.monotonic.return_value = 850
        poll = asyncio.create_task(entry.runtime_data.get_properties())
        await asyncio.wait_for(started.wait(), timeout=1)
        current_mode = 100
        finish.set()
        await poll

    get.reset_mock()
    with patch("custom_components.delonghi_dehumidifier_api.diagnostics.time") as clock:
        clock.time.side_effect = time.time
        clock.monotonic.return_value = 1000
        data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["properties"]["device_mode"]["value"] == 1
    assert current_mode == 100
    assert data["cache_age_seconds"] < 10
    assert data["request_age_seconds"] == 150
    get.assert_not_awaited()
    post.assert_not_awaited()


@pytest.mark.parametrize(
    "started",
    [None, -1, True, float("nan"), float("inf"), float("-inf"), SECRETS[2]],
)
async def test_invalid_request_start_is_not_reported(hass, diagnostics_entry, started):
    """Reject absent, invalid or non-finite monotonic timestamps."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties_request_started = started
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["request_age_seconds"] is None
    assert all(secret not in json.dumps(data) for secret in SECRETS)
    get.assert_not_awaited()
    post.assert_not_awaited()


async def test_future_or_missing_request_start_is_not_reported(hass, diagnostics_entry):
    """Neither future starts nor older clients without a start time are fresh."""
    entry, get, post = diagnostics_entry
    entry.runtime_data.device_properties_request_started = time.monotonic() + 3600
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["request_age_seconds"] is None
    del entry.runtime_data.device_properties_request_started
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["request_age_seconds"] is None
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
        "request_age_seconds": None,
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
