"""Exercise the fan speed select entity in Purifier and other modes."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.select import FanSpeedSelect
from tests.identity_helpers import bound_entry as MockConfigEntry


@pytest.fixture
async def device(hass):
    """Set up the integration against a mutable cloud snapshot."""
    properties = {
        "product_name": "Test",
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 54,
        "humidity_setpoint": 60,
        "current_speed": 1,
        "rotation_speed": 1,
        "filter_status": 1,
        "room_temp": 210,
        "heat_exchanger_temp": 210,
        "device_mode": 3,
        "device_status": 1,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
        "filter_change_alarm": 0,
        "filter_life": 100,
    }

    def snapshot():
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in properties.items()
        ]

    async def get_request(_path):
        return snapshot()

    entry = MockConfigEntry(
        dsn="TEST-DSN",
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
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="TEST-DSN")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["TEST-DSN"])),
        patch.object(
            APIClient, "get_request", AsyncMock(side_effect=get_request)
        ) as get,
        patch.object(APIClient, "post_request", AsyncMock(return_value={})) as post,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        fan_speed = registry.async_get_entity_id(
            "select", DOMAIN, f"{DOMAIN}_TEST-DSN_fan_speed_select"
        )
        assert fan_speed
        yield entry, properties, snapshot, get, post, fan_speed
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def refresh(hass, entry, *entity_ids):
    """Expire the cloud snapshot and invoke Home Assistant's update service."""
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": list(entity_ids)}, blocking=True
    )


async def test_fan_speed_select_and_mode_availability(hass, device):
    """Fan speed is selectable in Purifier mode and unavailable in other modes."""
    entry, properties, _, _, post, fan_speed = device
    assert hass.states.get(fan_speed).state == "1"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": fan_speed, "option": "3"},
        blocking=True,
    )
    assert hass.states.get(fan_speed).state == "3"
    post.assert_awaited_once_with(
        "apiv1/dsns/TEST-DSN/properties/rotation_speed/datapoints.json",
        {"datapoint": {"value": 3}},
    )

    # Immediate HA update keeps optimistic value without hitting cloud.
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": fan_speed}, blocking=True
    )
    assert hass.states.get(fan_speed).state == "3"

    # Delayed ActionRefresh within settle window keeps rotation_speed.
    properties.update(current_speed=1, rotation_speed=3)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(fan_speed).state == "3"

    # Switching to Dehumidify makes fan speed unavailable and blocks writes.
    properties["device_mode"] = 1
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == STATE_UNAVAILABLE

    before_calls = post.await_count
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": fan_speed, "option": "2"},
        blocking=True,
    )
    assert post.await_count == before_calls
    assert hass.states.get(fan_speed).state == STATE_UNAVAILABLE

    # Returning to Purifier restores availability.
    properties.update(device_mode=3, current_speed=2, rotation_speed=2)
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == "2"


async def test_fan_speed_invalid_option_rejected(hass, device):
    """Direct entity calls reject unsupported speed options."""
    entry, _, _, _, _, _ = device
    entity = FanSpeedSelect("TEST-DSN", AsyncMock(), entry.runtime_data)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("9")


async def test_fan_speed_non_purifier_mode_rejected(hass, device):
    """Direct entity calls reject speed writes outside Purifier mode."""
    entry, properties, _, _, post, _ = device
    properties["device_mode"] = 1
    entry.runtime_data.invalidate_properties()
    entity = FanSpeedSelect("TEST-DSN", AsyncMock(), entry.runtime_data)
    with pytest.raises(ServiceValidationError) as exc_info:
        await entity.async_select_option("2")
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "fan_speed_purifier_only"
    post.assert_not_awaited()


async def test_fan_speed_fallback_unknown_and_poll_recovery(hass, device):
    """Fallback to rotation_speed when idle, unknown if unset, and recover on errors."""
    entry, properties, _, get, _, fan_speed = device

    # Idle (current_speed=0) falls back to rotation_speed.
    properties.update(current_speed=0, rotation_speed=2)
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == "2"

    # Invalid speeds become unknown.
    properties.update(current_speed=0, rotation_speed=0)
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == STATE_UNKNOWN

    # Poll failure marks unavailable and recovers on next valid snapshot.
    get.side_effect = HomeAssistantError("cloud read failed")
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == STATE_UNAVAILABLE

    async def get_ok(_path):
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in properties.items()
        ]

    properties.update(current_speed=1, rotation_speed=1)
    get.side_effect = get_ok
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(fan_speed).state == "1"


@pytest.mark.parametrize("poll_fails", [False, True])
async def test_slow_poll_does_not_block_or_undo_command(hass, device, poll_fails):
    """A slow or failed pre-command poll cannot block or undo a command."""
    entry, properties, snapshot, get, post, fan_speed = device
    started = asyncio.Event()
    release = asyncio.Event()
    posted = asyncio.Event()
    old_snapshot = snapshot()

    async def delayed_fetch(_path):
        started.set()
        await release.wait()
        if poll_fails:
            raise HomeAssistantError("cloud read failed")
        return old_snapshot

    async def successful_write(_path, _body):
        posted.set()
        return {}

    get.side_effect = delayed_fetch
    post.side_effect = successful_write
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": fan_speed}, blocking=True
        )
    )
    command = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        command = asyncio.create_task(
            hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": fan_speed, "option": "3"},
                blocking=True,
            )
        )
        await asyncio.wait_for(posted.wait(), 2)
        await asyncio.wait_for(asyncio.shield(command), 2)
        assert hass.states.get(fan_speed).state == "3"
    finally:
        release.set()
        await poll
        if command is not None:
            await command

    assert hass.states.get(fan_speed).state == "3"

    async def fresh_fetch(_path):
        return snapshot()

    get.side_effect = fresh_fetch
    properties.update(current_speed=2, rotation_speed=2)
    await refresh(hass, entry, fan_speed)
    assert hass.states.get(fan_speed).state == "2"
