"""Exercise shared property reads and command safety through Home Assistant."""

import asyncio
import logging
import traceback
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_switch_refresh
from tests.test_client_http import make_client

device = test_switch_refresh.device


def entity_id(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{DOMAIN}_TEST-DSN_{suffix}"
    )


async def test_concurrent_entities_share_one_uncached_snapshot(hass, device):
    entry, _, snapshot, get, _, eco, swing, eco_sensor = device
    alarm = entity_id(hass, "binary_sensor", "device_alarm_binary_sensor")
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    started = asyncio.Event()
    release = asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    get.reset_mock()
    entry.runtime_data.device_properties_timestamp = 0
    polls = [
        asyncio.create_task(async_update_entity(hass, item))
        for item in (alarm, humidity, eco, swing, eco_sensor)
    ]
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.02)
    finally:
        release.set()
        await asyncio.gather(*polls)
    assert get.await_count == 1
    assert hass.states.get(humidity).state == "54"
    assert hass.states.get(eco).state == "off"


async def test_cancelling_first_reader_preserves_later_reader(hass, device):
    entry, _, snapshot, get, _, _, _, _ = device
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    alarm = entity_id(hass, "binary_sensor", "device_alarm_binary_sensor")
    started = asyncio.Event()
    release = asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    get.reset_mock()
    entry.runtime_data.device_properties_timestamp = 0
    first = asyncio.create_task(async_update_entity(hass, humidity))
    second = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(async_update_entity(hass, alarm))
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
        if second is not None:
            await second
    assert get.await_count == 1
    assert hass.states.get(alarm).state == "off"


async def test_failed_shared_read_can_retry():
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    started = asyncio.Event()
    release = asyncio.Event()

    async def failed(_path):
        started.set()
        await release.wait()
        raise aiohttp.ClientConnectionError("temporary")

    client.get_request = AsyncMock(side_effect=failed)
    readers = [asyncio.create_task(client.get_properties()) for _ in range(5)]
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.sleep(0.02)
    release.set()
    errors = await asyncio.gather(*readers, return_exceptions=True)
    assert all(isinstance(err, aiohttp.ClientConnectionError) for err in errors)
    assert client.get_request.await_count == 1
    client.get_request.side_effect = None
    client.get_request.return_value = [{"property": {"name": "swing", "value": 1}}]
    assert await client.get_properties() == [{"name": "swing", "value": 1}]
    assert client.get_request.await_count == 2


async def test_precommand_shared_read_does_not_overwrite_accepted_switch(hass, device):
    entry, _, snapshot, get, _, eco, _, eco_sensor = device
    started = asyncio.Event()
    release = asyncio.Event()
    old_snapshot = snapshot()

    async def read(_path):
        started.set()
        await release.wait()
        return old_snapshot

    get.side_effect = read
    get.reset_mock()
    entry.runtime_data.device_properties_timestamp = 0
    old_poll = asyncio.create_task(async_update_entity(hass, eco_sensor))
    switch_poll = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        switch_poll = asyncio.create_task(async_update_entity(hass, eco))
        await asyncio.sleep(0.02)
    finally:
        release.set()
        await old_poll
        if switch_poll is not None:
            await switch_poll
    assert get.await_count == 1
    assert hass.states.get(eco).state == "on"


async def test_abandoned_old_request_cannot_replace_new_snapshot():
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    old_finished = asyncio.Event()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            old_finished.set()
            return [{"property": {"name": "swing", "value": 0}}]
        return [{"property": {"name": "swing", "value": 1}}]

    client.get_request = AsyncMock(side_effect=read)
    first = asyncio.create_task(client.get_properties())
    await asyncio.wait_for(started.wait(), 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        assert await client.get_properties() == [{"name": "swing", "value": 1}]
    finally:
        release.set()
        await asyncio.wait_for(old_finished.wait(), 2)
    await asyncio.sleep(0)
    assert client.device_properties == [{"name": "swing", "value": 1}]


@pytest.mark.parametrize(
    "failure", [aiohttp.ClientConnectionError, TimeoutError, HomeAssistantError]
)
async def test_shared_failure_is_safe_even_when_the_runtime_formats_the_task(failure):
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    client.get_request = AsyncMock(side_effect=failure("secret-cloud-detail"))
    with pytest.raises(failure) as error:
        await client.get_properties()
    assert "secret-cloud-detail" not in repr(error.value)
    assert "secret-cloud-detail" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    ("status", "kind"), [(307, HomeAssistantError), (429, TransientCloudError)]
)
async def test_shared_failure_keeps_the_numeric_http_status(status, kind):
    client, session, _ = make_client(status, {"detail": "private-cloud-payload"})
    with pytest.raises(kind, match=f"HTTP {status}") as error:
        await client.get_properties()
    assert "private-cloud-payload" not in "".join(
        traceback.format_exception(error.value)
    )
    assert len(session.calls) == 1


@pytest.mark.parametrize("malformed", [{}, "", None])
async def test_outage_only_recovers_on_a_valid_property_list(malformed, caplog):
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    snapshot = [{"property": {"name": "swing", "value": 1}}]
    client.get_request = AsyncMock(
        side_effect=[snapshot, TimeoutError(), malformed, []]
    )
    assert await client.get_properties() == [{"name": "swing", "value": 1}]
    client.invalidate_properties()
    caplog.set_level(logging.INFO, logger=client.__module__)
    with pytest.raises(TimeoutError):
        await client.get_properties()
    with pytest.raises(HomeAssistantError):
        await client.get_properties()
    assert client.device_properties == [{"name": "swing", "value": 1}]
    assert caplog.messages == ["DeLonghi cloud device data is unavailable"]
    assert await client.get_properties() == []
    assert caplog.messages == [
        "DeLonghi cloud device data is unavailable",
        "DeLonghi cloud device data is available again",
    ]


@pytest.mark.parametrize("initial_outage", [False, True])
async def test_obsolete_result_cannot_announce_a_connectivity_transition(
    initial_outage, caplog
):
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    client.get_request = AsyncMock(return_value=[])
    await client.get_properties()
    client.invalidate_properties()
    caplog.set_level(logging.INFO, logger=client.__module__)
    if initial_outage:
        client.get_request.side_effect = TimeoutError()
        with pytest.raises(TimeoutError):
            await client.get_properties()
        caplog.clear()

    started, release = asyncio.Event(), asyncio.Event()

    async def stale_read(_path):
        started.set()
        await release.wait()
        if not initial_outage:
            raise TimeoutError()
        return []

    client.get_request.side_effect = stale_read
    task = asyncio.create_task(client.get_properties())
    try:
        await asyncio.wait_for(started.wait(), 2)
        client.invalidate_properties()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not caplog.records

    client.get_request.side_effect = None if initial_outage else TimeoutError()
    if initial_outage:
        await client.get_properties()
        expected = "DeLonghi cloud device data is available again"
    else:
        with pytest.raises(TimeoutError):
            await client.get_properties()
        expected = "DeLonghi cloud device data is unavailable"
    assert caplog.messages == [expected]


async def test_abandoned_request_exception_is_consumed(caplog):
    client = APIClient(None, "en", "test@example.com", "password")
    client.device_dsn = "test-dsn"
    started = asyncio.Event()
    finished = asyncio.Event()
    client.get_request = AsyncMock(return_value=[])
    await client.get_properties()
    client.invalidate_properties()
    caplog.set_level(logging.INFO, logger=client.__module__)

    async def read(_path):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            finished.set()
            raise aiohttp.ClientConnectionError("failed while cancelling")

    client.get_request = AsyncMock(side_effect=read)
    loop = asyncio.get_running_loop()
    with pytest.MonkeyPatch.context() as monkeypatch:
        errors = []
        monkeypatch.setattr(loop, "call_exception_handler", errors.append)
        caller = asyncio.create_task(client.get_properties())
        await asyncio.wait_for(started.wait(), 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not errors
        assert not caplog.records
