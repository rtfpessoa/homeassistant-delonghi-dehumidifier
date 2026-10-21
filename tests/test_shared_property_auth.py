"""Exercise shared property reads across authentication and unload."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_runtime_reauth, test_switch_refresh

loaded_entry = test_runtime_reauth.loaded_entry
device = test_switch_refresh.device


async def test_simultaneous_rejected_polls_share_one_request_and_reauth(
    hass, loaded_entry
):
    entry, session, sensor_ids = loaded_entry
    client = entry.runtime_data
    original_get = client.get_request
    session.pause = True
    with patch.object(client, "get_request", AsyncMock(wraps=original_get)) as get:
        polls = [
            asyncio.create_task(async_update_entity(hass, entity_id))
            for entity_id in sensor_ids
        ]
        try:
            await asyncio.wait_for(session.login_started.wait(), 2)
            await asyncio.sleep(0.02)
        finally:
            session.continue_login.set()
            await asyncio.gather(*polls)
        await hass.async_block_till_done()
        assert get.await_count == 1
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    assert all(hass.states.get(entity).state == "unavailable" for entity in sensor_ids)


async def test_unload_cancels_shared_property_request(hass, device):
    entry, _, snapshot, get, _, _, _, _ = device
    client = entry.runtime_data
    started = asyncio.Event()
    release = asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    client.device_properties_timestamp = 0
    readers = [asyncio.create_task(client.get_properties()) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.02)
        assert await hass.config_entries.async_unload(entry.entry_id)
        results = await asyncio.wait_for(
            asyncio.gather(*readers, return_exceptions=True), 2
        )
    finally:
        release.set()
        await asyncio.gather(*readers, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


async def test_inflight_success_is_not_published_after_rejection(hass, loaded_entry):
    entry, session, _ = loaded_entry
    client = entry.runtime_data
    started = asyncio.Event()
    release = asyncio.Event()
    previous_timestamp = client.device_properties_timestamp

    async def delayed(_path):
        started.set()
        await release.wait()
        return [{"property": {"name": "swing", "value": 1}}]

    with patch.object(client, "get_request", side_effect=delayed):
        read = asyncio.create_task(client.get_properties())
        try:
            await asyncio.wait_for(started.wait(), 2)
            with pytest.raises(InvalidCredentialsError):
                await client.get_access_token()
        finally:
            release.set()
        with pytest.raises(InvalidCredentialsError):
            await read
    assert client.device_properties_timestamp == previous_timestamp
    assert len(session.logins) == 1


async def test_unload_cancels_a_real_entity_poll(hass, device):
    entry, _, snapshot, get, _, _, _, _ = device
    humidity = _humidity_entity_id(hass)
    started = asyncio.Event()
    release = asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(async_update_entity(hass, humidity))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert await asyncio.wait_for(
            hass.config_entries.async_unload(entry.entry_id), 2
        )
        result = await asyncio.wait_for(asyncio.gather(poll, return_exceptions=True), 2)
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)


def _humidity_entity_id(hass):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_current_humidity_sensor"
    )
