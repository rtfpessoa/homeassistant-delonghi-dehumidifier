"""Exercise shared humidifier commands through the loaded integration."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from tests import test_humidifier_sync, test_switch_refresh

device = test_switch_refresh.device
humidifier_id = test_humidifier_sync.humidifier_id


async def test_other_caller_waits_for_humidifier_command(hass, device):
    """A guarded caller cannot inspect or write while an HA command is pending."""
    entry, _, _, _, post, *_ = device
    commands = entry.runtime_data.humidifier_commands
    started, release, guard_started = (asyncio.Event() for _ in range(3))
    send = AsyncMock(return_value={})

    async def write(_path, _data):
        started.set()
        await release.wait()
        return {}

    async def guard():
        guard_started.set()

    post.side_effect = write
    first = asyncio.create_task(
        hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": humidifier_id(hass)}, blocking=True
        )
    )
    second = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        second = asyncio.create_task(commands.run(send, guard=guard))
        await asyncio.sleep(0.02)
        assert not guard_started.is_set()
        send.assert_not_awaited()
        release.set()
        await asyncio.gather(first, second)
        assert guard_started.is_set()
        send.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


async def test_humidifier_command_waits_for_other_callers_guard(hass, device):
    """The guard and write are atomic with respect to existing HA commands."""
    entry, _, _, _, post, *_ = device
    started, release = asyncio.Event(), asyncio.Event()
    send = AsyncMock(return_value={})

    async def guard():
        started.set()
        await release.wait()

    first = asyncio.create_task(
        entry.runtime_data.humidifier_commands.run(send, guard=guard)
    )
    second = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        second = asyncio.create_task(
            hass.services.async_call(
                "humidifier",
                "turn_off",
                {"entity_id": humidifier_id(hass)},
                blocking=True,
            )
        )
        await asyncio.sleep(0.02)
        post.assert_not_awaited()
        release.set()
        await asyncio.gather(first, second)
        send.assert_awaited_once()
        post.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.parametrize("outcome", ["reject", "cancel"])
async def test_guard_failure_does_not_attempt_a_write(hass, device, outcome):
    """A rejected or canceled guard leaves the command generation unchanged."""
    entry, _, _, get, post, *_ = device
    client = entry.runtime_data
    commands = client.humidifier_commands
    generations = commands.generation, client._properties_generation
    reads = get.await_count
    started = asyncio.Event()
    send = AsyncMock(return_value={})

    async def guard():
        started.set()
        if outcome == "reject":
            raise ServiceValidationError("Condition not met")
        await asyncio.Event().wait()

    task = asyncio.create_task(commands.run(send, guard=guard))
    await asyncio.wait_for(started.wait(), 3)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ServiceValidationError, match="Condition not met"):
            await task
    await hass.async_block_till_done()
    assert (commands.generation, client._properties_generation) == generations
    assert get.await_count == reads
    post.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["success", "timeout"])
async def test_other_caller_refreshes_after_an_old_poll(hass, device, outcome):
    """Another platform's command cannot let an earlier observation win."""
    entry, properties, snapshot, get, _, *_ = device
    client = entry.runtime_data
    entity_id = humidifier_id(hass)
    started, release, fresh = (asyncio.Event() for _ in range(3))
    previous = snapshot()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return previous
        fresh.set()
        return snapshot()

    async def send():
        properties["device_status"] = 2
        if outcome == "timeout":
            raise TimeoutError("response lost")
        return {}

    get.side_effect = read
    client.device_properties_timestamp = 0
    poll = asyncio.create_task(async_update_entity(hass, entity_id))
    try:
        await asyncio.wait_for(started.wait(), 3)
        if outcome == "timeout":
            with pytest.raises(TimeoutError, match="response lost"):
                await client.humidifier_commands.run(send, refresh_after_success=True)
        else:
            await client.humidifier_commands.run(send, refresh_after_success=True)
        release.set()
        await poll
        await asyncio.wait_for(fresh.wait(), 3)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(entity_id).state == "off"
        assert calls == 2
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)


async def test_cancelled_other_caller_refreshes_humidifier(hass, device):
    """A canceled shared write can have reached the cloud and needs a refresh."""
    entry, properties, _, get, _, *_ = device
    entity_id = humidifier_id(hass)
    started = asyncio.Event()
    reads = get.await_count

    async def send():
        properties["device_status"] = 2
        started.set()
        await asyncio.Event().wait()

    command = asyncio.create_task(entry.runtime_data.humidifier_commands.run(send))
    await asyncio.wait_for(started.wait(), 3)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id).state == "off"
    assert get.await_count == reads + 1


async def test_unload_unsubscribes_refresh_from_inflight_command(hass, device):
    """Finishing a shared command after unload cannot retain or refresh the entity."""
    entry, _, _, get, _, *_ = device
    client = entry.runtime_data
    commands = client.humidifier_commands
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    started, release = asyncio.Event(), asyncio.Event()

    async def send():
        started.set()
        await release.wait()
        raise TimeoutError("response lost")

    command = asyncio.create_task(commands.run(send))
    try:
        await asyncio.wait_for(started.wait(), 3)
        with patch.object(entity, "async_schedule_update_ha_state") as schedule:
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert not commands._refresh_listeners
            get.reset_mock()
            release.set()
            with pytest.raises(TimeoutError, match="response lost"):
                await command
            await hass.async_block_till_done(wait_background_tasks=True)
            schedule.assert_not_called()
            get.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(command, return_exceptions=True)


async def test_shared_commands_exist_with_humidifier_disabled(hass, device):
    """Other platforms can use entry-scoped commands without the entity enabled."""
    entry, _, _, _, _, *_ = device
    entity_id = humidifier_id(hass)
    registry = er.async_get(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    commands = entry.runtime_data.humidifier_commands
    assert registry.async_get(entity_id).disabled_by == er.RegistryEntryDisabler.USER
    assert not commands._refresh_listeners
    send = AsyncMock(return_value={})
    await commands.run(send, refresh_after_success=True)
    send.assert_awaited_once()
