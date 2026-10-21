"""Exercise switch state refreshes and writes through Home Assistant."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.switch import GenericOffOnSwitchSensor


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
        "filter_status": 1,
        "room_temp": 210,
        "heat_exchanger_temp": 210,
        "device_mode": 1,
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
        patch.object(
            APIClient, "get_request", AsyncMock(side_effect=get_request)
        ) as get,
        patch.object(APIClient, "post_request", AsyncMock(return_value={})) as post,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        eco = registry.async_get_entity_id(
            "switch", DOMAIN, f"{DOMAIN}_TEST-DSN_eco_mode_switch"
        )
        swing = registry.async_get_entity_id(
            "switch", DOMAIN, f"{DOMAIN}_TEST-DSN_swing_mode_switch"
        )
        eco_sensor = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_eco_mode_sensor"
        )
        assert eco and swing and eco_sensor
        yield entry, properties, snapshot, get, post, eco, swing, eco_sensor
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def refresh(hass, entry, *entity_ids):
    """Expire the cloud snapshot and invoke Home Assistant's update service."""
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": list(entity_ids)}, blocking=True
    )


async def test_external_changes_are_reflected(hass, device):
    """Observed external Eco and Swing changes reach their switch states."""
    entry, properties, _, _, _, eco, swing, _ = device
    assert hass.states.get(eco).state == STATE_OFF
    assert hass.states.get(swing).state == STATE_OFF

    properties["set_eco"] = 1
    await refresh(hass, entry, eco, swing)
    assert hass.states.get(eco).state == STATE_ON
    assert hass.states.get(swing).state == STATE_OFF

    properties.update(set_eco=0, swing=1)
    entry.runtime_data.device_properties_timestamp = 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(eco).state == STATE_OFF
    assert hass.states.get(swing).state == STATE_ON


async def test_switch_and_device_alarm_share_poll_results(hass, device):
    """Switch refreshes coexist with the DDSX220 device alarm."""
    entry, properties, _, _, _, eco, _, _ = device
    registry = er.async_get(hass)
    alarm = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_device_alarm_binary_sensor"
    )
    assert alarm
    assert hass.states.get(alarm).state == STATE_OFF
    properties.update(set_eco=1, alarm_state=3)
    await refresh(hass, entry, eco, alarm)
    assert hass.states.get(eco).state == STATE_ON
    assert hass.states.get(alarm).state == STATE_ON


async def test_command_is_not_overwritten_by_old_cache(hass, device):
    """An accepted command survives an immediate poll of the old snapshot."""
    entry, properties, _, get, post, eco, _, _ = device
    assert hass.states.get(eco).state == STATE_OFF
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": eco}, blocking=True
    )
    assert hass.states.get(eco).state == STATE_ON
    post.assert_awaited_once()
    requests = get.await_count
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
    )
    assert get.await_count == requests
    assert hass.states.get(eco).state == STATE_ON

    properties["set_eco"] = 1
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_ON

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eco}, blocking=True
    )
    assert hass.states.get(eco).state == STATE_OFF
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
    )
    assert hass.states.get(eco).state == STATE_OFF

    properties["set_eco"] = 0
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_OFF


async def test_automatic_refresh_does_not_fetch_after_command(hass, device):
    """The automatic refresh keeps the accepted state even when cache expired."""
    entry, _, _, get, _, eco, _, _ = device
    entry.runtime_data.device_properties_timestamp = 0
    requests = get.await_count
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": eco}, blocking=True
    )
    assert get.await_count == requests
    assert hass.states.get(eco).state == STATE_ON


@pytest.mark.parametrize("value", [None, 9, -1])
async def test_unknown_values_and_recovery(hass, device, value):
    """Missing or unrecognized values become unknown and can recover."""
    entry, properties, _, _, _, eco, swing, _ = device
    properties["set_eco"] = value
    properties.pop("swing")
    await refresh(hass, entry, eco, swing)
    assert hass.states.get(eco).state == STATE_UNKNOWN
    assert hass.states.get(swing).state == STATE_UNKNOWN
    properties.update(set_eco=1, swing=1)
    await refresh(hass, entry, eco, swing)
    assert hass.states.get(eco).state == STATE_ON
    assert hass.states.get(swing).state == STATE_ON


async def test_failed_poll_is_unavailable_and_recovers(hass, device):
    """A cloud read failure must not leave an old state displayed."""
    entry, properties, _, get, _, eco, _, _ = device
    get.side_effect = HomeAssistantError("cloud read failed")
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_UNAVAILABLE
    properties["set_eco"] = 1

    async def get_request(_path):
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in properties.items()
        ]

    get.side_effect = get_request
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_ON


async def test_failed_command_keeps_state_and_recovers(hass, device):
    """A rejected command cannot be displayed as an accepted change."""
    entry, properties, _, _, post, eco, _, _ = device
    post.side_effect = HomeAssistantError("cloud write failed")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
    assert hass.states.get(eco).state == STATE_OFF
    properties["set_eco"] = 1
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_ON


async def test_older_inflight_fetch_does_not_overwrite_command(hass, device):
    """A pre-command request completing later cannot undo a command."""
    entry, properties, snapshot, get, _, eco, _, eco_sensor = device
    started = asyncio.Event()
    release = asyncio.Event()
    old_snapshot = snapshot()

    async def delayed_fetch(_path):
        started.set()
        await release.wait()
        return old_snapshot

    get.side_effect = delayed_fetch
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": eco_sensor}, blocking=True
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            ),
            2,
        )
        assert hass.states.get(eco).state == STATE_ON
    finally:
        release.set()
        await poll

    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
    )
    assert hass.states.get(eco).state == STATE_ON

    async def fresh_fetch(_path):
        return snapshot()

    get.side_effect = fresh_fetch
    properties["set_eco"] = 1
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_ON
    properties["set_eco"] = 0
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_OFF


@pytest.mark.parametrize("poll_fails", [False, True])
async def test_slow_poll_does_not_block_or_undo_command(hass, device, poll_fails):
    """A slow or failed old poll cannot block or undo an accepted command."""
    entry, properties, snapshot, get, post, eco, _, _ = device
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
            "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
        )
    )
    command = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        command = asyncio.create_task(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
        )
        await asyncio.wait_for(posted.wait(), 2)
        await asyncio.wait_for(asyncio.shield(command), 2)
        assert hass.states.get(eco).state == STATE_ON
    finally:
        release.set()
        await poll
        if command is not None:
            await command

    assert hass.states.get(eco).state == STATE_ON

    async def fresh_fetch(_path):
        return snapshot()

    get.side_effect = fresh_fetch
    properties["set_eco"] = 0
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_OFF


@pytest.mark.parametrize("poll_finishes_first", [False, True])
@pytest.mark.parametrize("write_succeeds", [False, True])
async def test_failed_poll_and_command_overlap(
    hass, device, poll_finishes_first, write_succeeds
):
    """Only a successful overlapping write recovers a failed poll's availability."""
    entry, _, snapshot, get, post, eco, _, _ = device
    poll_started = asyncio.Event()
    write_started = asyncio.Event()
    release_poll = asyncio.Event()
    release_write = asyncio.Event()

    async def failed_fetch(_path):
        poll_started.set()
        await release_poll.wait()
        raise HomeAssistantError("cloud read failed")

    async def delayed_write(_path, _body):
        write_started.set()
        await release_write.wait()
        if not write_succeeds:
            raise HomeAssistantError("cloud write failed")
        return {}

    get.side_effect = failed_fetch
    post.side_effect = delayed_write
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
        )
    )
    command = None
    try:
        await asyncio.wait_for(poll_started.wait(), 2)
        command = asyncio.create_task(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
        )
        await asyncio.wait_for(write_started.wait(), 2)
        if poll_finishes_first:
            release_poll.set()
            await poll
            assert hass.states.get(eco).state == STATE_UNAVAILABLE
        release_write.set()
        if write_succeeds:
            await command
        else:
            with pytest.raises(HomeAssistantError):
                await command
        if not poll_finishes_first:
            release_poll.set()
            await poll
        assert hass.states.get(eco).state == (
            STATE_ON if write_succeeds else STATE_UNAVAILABLE
        )
    finally:
        release_poll.set()
        release_write.set()
        if not poll.done():
            await poll
        if command is not None and not command.done():
            try:
                await command
            except HomeAssistantError:
                pass

    async def fresh_fetch(_path):
        return snapshot()

    get.side_effect = fresh_fetch
    await refresh(hass, entry, eco)
    assert hass.states.get(eco).state == STATE_OFF


async def test_poll_during_command_cannot_overwrite_accepted_state(hass, device):
    """A poll overlapping a write cannot restore the old state after success."""
    entry, _, _, _, post, eco, _, _ = device
    entry.runtime_data.device_properties_timestamp = 0
    started = asyncio.Event()
    poll_started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_write(_path, _body):
        started.set()
        await release.wait()
        return {}

    post.side_effect = delayed_write
    original_update = GenericOffOnSwitchSensor.async_update

    async def observed_update(entity):
        poll_started.set()
        await original_update(entity)

    command = asyncio.create_task(
        hass.services.async_call("switch", "turn_on", {"entity_id": eco}, blocking=True)
    )
    with patch.object(GenericOffOnSwitchSensor, "async_update", observed_update):
        try:
            await asyncio.wait_for(started.wait(), 2)
            poll = asyncio.create_task(
                hass.services.async_call(
                    "homeassistant", "update_entity", {"entity_id": eco}, blocking=True
                )
            )
            await asyncio.wait_for(poll_started.wait(), 2)
        finally:
            release.set()
            await command
        await poll
    assert hass.states.get(eco).state == STATE_ON
