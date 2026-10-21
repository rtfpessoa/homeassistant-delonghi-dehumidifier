"""Exercise humidifier writes and cloud observations through Home Assistant."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_runtime_reauth, test_switch_refresh

device = test_switch_refresh.device
loaded_entry = test_runtime_reauth.loaded_entry


def humidifier_id(hass):
    """Find the registered humidifier."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_TEST-DSN_dehumidifier"
    )
    assert entity_id
    return entity_id


@pytest.mark.parametrize(
    ("service", "data", "property_name", "new_value", "attribute", "expected"),
    [
        ("turn_off", {}, "device_status", 2, None, "off"),
        ("set_mode", {"mode": "DRY_CLOTHES"}, "device_mode", 2, "mode", "DRY_CLOTHES"),
        ("set_humidity", {"humidity": 45}, "humidity_setpoint", 45, "humidity", 45),
    ],
)
async def test_successful_command_observes_a_fresh_snapshot(
    hass, device, service, data, property_name, new_value, attribute, expected
):
    """The service refresh reads the cloud after each accepted command."""
    _, properties, _, get, post, *_ = device
    entity_id = humidifier_id(hass)
    requests = get.await_count

    async def write(_path, _data):
        properties[property_name] = new_value
        return {}

    post.side_effect = write
    await hass.services.async_call(
        "humidifier", service, {"entity_id": entity_id, **data}, blocking=True
    )
    state = hass.states.get(entity_id)
    assert (
        state.state if attribute is None else state.attributes[attribute]
    ) == expected
    assert get.await_count == requests + 1


async def test_accepted_command_does_not_claim_a_physical_change(hass, device):
    """A fresh observation may still report the previous physical state."""
    _, _, _, get, _, *_ = device
    entity_id = humidifier_id(hass)
    requests = get.await_count
    assert hass.states.get(entity_id).state == "on"
    await hass.services.async_call(
        "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == "on"
    assert get.await_count == requests + 1


async def test_turn_on_observes_the_cloud(hass, device):
    """Turn-on has the same observation behavior as turn-off."""
    entry, properties, _, get, post, *_ = device
    entity_id = humidifier_id(hass)
    properties["device_status"] = 2
    await test_switch_refresh.refresh(hass, entry, entity_id)
    assert hass.states.get(entity_id).state == "off"
    requests = get.await_count

    async def write(_path, _data):
        properties["device_status"] = 1
        return {}

    post.side_effect = write
    await hass.services.async_call(
        "humidifier", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == "on"
    assert get.await_count == requests + 1


@pytest.mark.parametrize("old_result", ["success", "failure", "cancel"])
async def test_old_humidifier_poll_is_followed_by_fresh_read(hass, device, old_result):
    """HA skipping its staged automatic refresh must not leave stale state."""
    entry, properties, snapshot, get, post, *_ = device
    entity_id = humidifier_id(hass)
    started, release, fresh = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_snapshot = snapshot()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            if old_result == "failure":
                raise HomeAssistantError("old request failed")
            return old_snapshot
        fresh.set()
        return snapshot()

    async def write(_path, _data):
        properties["device_status"] = 2
        return {}

    get.side_effect = read
    post.side_effect = write
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        await hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).state == "on"
        if old_result == "cancel":
            poll.cancel()
            with pytest.raises(asyncio.CancelledError):
                await poll
        else:
            release.set()
            await poll
        await asyncio.wait_for(fresh.wait(), 3)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(entity_id).state == "off"
    finally:
        release.set()
        if not poll.done():
            poll.cancel()


@pytest.mark.parametrize("old_fails", [False, True])
async def test_old_shared_read_drains_before_fresh_observation(hass, device, old_fails):
    """Other entities keep their shared read, then all new readers share a fresh one."""
    entry, properties, snapshot, get, post, eco, _, sensor = device
    entity_id = humidifier_id(hass)
    registry = er.async_get(hass)
    humidity = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_current_humidity_sensor"
    )
    alarm = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_device_alarm_binary_sensor"
    )
    assert humidity and alarm
    started, release, posted, fresh_started = (asyncio.Event() for _ in range(4))
    old_snapshot = snapshot()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            if old_fails:
                raise HomeAssistantError("old shared read failed")
            return old_snapshot
        fresh_started.set()
        return snapshot()

    async def write(_path, _data):
        properties.update(device_status=2, set_eco=1)
        posted.set()
        return {}

    get.side_effect = read
    post.side_effect = write
    entry.runtime_data.device_properties_timestamp = 0
    old = asyncio.create_task(async_update_entity(hass, sensor))
    command = None
    new_readers = []
    try:
        await asyncio.wait_for(started.wait(), 3)
        command = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
            )
        )
        await asyncio.wait_for(posted.wait(), 3)
        new_readers = [
            asyncio.create_task(async_update_entity(hass, item))
            for item in (eco, humidity, alarm)
        ]
        await asyncio.sleep(0.02)
        assert not old.done()
        assert not command.done()
        assert not fresh_started.is_set()
        assert calls == 1
        release.set()
        await asyncio.gather(old, command, *new_readers)
        assert calls == 2
        assert hass.states.get(entity_id).state == "off"
        assert hass.states.get(sensor).state == ("unavailable" if old_fails else "OFF")
        assert hass.states.get(eco).state == "on"
        assert hass.states.get(humidity).state == "54"
        assert hass.states.get(alarm).state == "off"
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (old, command, *new_readers) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
async def test_unsuccessful_command_keeps_old_read_eligible(hass, device, outcome):
    """A rejected or canceled command must not discard a valid observation."""
    entry, properties, snapshot, get, post, *_ = device
    entity_id = humidifier_id(hass)
    read_started, release, write_started = (asyncio.Event() for _ in range(3))
    properties["device_status"] = 2

    async def read(_path):
        read_started.set()
        await release.wait()
        return snapshot()

    async def write(_path, _data):
        write_started.set()
        if outcome == "failure":
            raise HomeAssistantError("rejected")
        await asyncio.Event().wait()

    get.side_effect = read
    post.side_effect = write
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(async_update_entity(hass, entity_id))
    command = None
    try:
        await asyncio.wait_for(read_started.wait(), 3)
        command = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
            )
        )
        await asyncio.wait_for(write_started.wait(), 3)
        if outcome == "cancel":
            command.cancel()
            with pytest.raises(asyncio.CancelledError):
                await command
        else:
            with pytest.raises(HomeAssistantError):
                await command
        release.set()
        await poll
        assert hass.states.get(entity_id).state == "off"
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (poll, command) if task is not None),
            return_exceptions=True,
        )


async def test_humidifier_commands_are_serialized(hass, device):
    """A later write cannot reach the cloud before an earlier one completes."""
    _, properties, _, _, post, *_ = device
    entity_id = humidifier_id(hass)
    first_started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(_path, data):
        value = data["datapoint"]["value"]
        calls.append(value)
        if len(calls) == 1:
            first_started.set()
            await release.wait()
        properties["device_status"] = value
        return {}

    post.side_effect = write
    first = asyncio.create_task(
        hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
        )
    )
    second = None
    try:
        await asyncio.wait_for(first_started.wait(), 3)
        second = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_on", {"entity_id": entity_id}, blocking=True
            )
        )
        await asyncio.sleep(0.02)
        assert calls == [2]
        release.set()
        await asyncio.gather(first, second)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert calls == [2, 1]
        assert hass.states.get(entity_id).state == "on"
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


async def test_cancelled_new_reader_does_not_cancel_old_shared_read(hass, device):
    """Canceling a post-command waiter must leave the earlier sensor GET alone."""
    entry, _, snapshot, get, _, _, _, sensor = device
    entity_id = humidifier_id(hass)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    entry.runtime_data.device_properties_timestamp = 0
    old = asyncio.create_task(async_update_entity(hass, sensor))
    command = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        command = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
            )
        )
        await asyncio.sleep(0.02)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert not old.done()
        release.set()
        await old
        assert hass.states.get(sensor).state == "OFF"
        await async_update_entity(hass, entity_id)
        assert hass.states.get(entity_id).state == "on"
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (old, command) if task is not None),
            return_exceptions=True,
        )


async def test_auth_rejection_while_waiting_for_old_read(hass, loaded_entry):
    """Stale completion cannot undo reauthentication availability."""
    entry, session, sensors = loaded_entry
    client = entry.runtime_data
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    assert entity_id
    started, release, posted = (asyncio.Event() for _ in range(3))

    async def read(_path):
        started.set()
        await release.wait()
        return [{"property": prop} for prop in test_runtime_reauth.PROPERTIES]

    async def write(_path, _data):
        posted.set()
        return {}

    with (
        patch.object(client, "get_request", AsyncMock(side_effect=read)),
        patch.object(client, "post_request", AsyncMock(side_effect=write)),
    ):
        client.device_properties_timestamp = 0
        old = asyncio.create_task(async_update_entity(hass, sensors[0]))
        command = None
        try:
            await asyncio.wait_for(started.wait(), 3)
            command = asyncio.create_task(
                hass.services.async_call(
                    "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
                )
            )
            await asyncio.wait_for(posted.wait(), 3)
            with pytest.raises(test_runtime_reauth.InvalidCredentialsError):
                await client.get_access_token()
            release.set()
            await asyncio.gather(old, command)
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "unavailable"
            assert len(session.logins) == 1
            assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (old, command) if task is not None),
                return_exceptions=True,
            )


async def test_unload_releases_post_command_waiters(hass, device):
    """Unload cancels the old GET and releases readers waiting for a fresh one."""
    entry, _, snapshot, get, post, _, _, sensor = device
    entity_id = humidifier_id(hass)
    started, posted = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await asyncio.Event().wait()
        return snapshot()

    async def write(_path, _data):
        posted.set()
        return {}

    get.side_effect = read
    post.side_effect = write
    entry.runtime_data.device_properties_timestamp = 0
    old = asyncio.create_task(async_update_entity(hass, sensor))
    command = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        command = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_off", {"entity_id": entity_id}, blocking=True
            )
        )
        await asyncio.wait_for(posted.wait(), 3)
        assert await asyncio.wait_for(
            hass.config_entries.async_unload(entry.entry_id), 3
        )
        await asyncio.wait_for(asyncio.gather(old, command, return_exceptions=True), 3)
    finally:
        for task in (old, command):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (old, command) if task is not None),
            return_exceptions=True,
        )
