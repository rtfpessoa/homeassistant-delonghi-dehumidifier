"""Exercise sensor and humidifier poll failures through Home Assistant."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.humidifier import DehumidifierEntity
from custom_components.delonghi_dehumidifier_api.sensor import GenericSensor
from tests import test_switch_refresh

device = test_switch_refresh.device


def entity_id(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{DOMAIN}_TEST-DSN_{suffix}"
    )


async def update(hass, entry, *entity_ids):
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": list(entity_ids)}, blocking=True
    )


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("secret-network-detail"),
        TimeoutError("secret-timeout-detail"),
        HomeAssistantError("secret-http-detail"),
    ],
)
async def test_poll_outage_and_recovery(hass, device, caplog, error):
    entry, properties, snapshot, get, _, _, _, _ = device
    humidifier = entity_id(hass, "humidifier", "dehumidifier")
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    assert hass.states.get(humidifier).state == "on"
    assert hass.states.get(humidity).state == "54"
    get.side_effect = error
    with caplog.at_level(logging.WARNING):
        await update(hass, entry, humidifier, humidity)
        assert hass.states.get(humidifier).state == STATE_UNAVAILABLE
        assert hass.states.get(humidity).state == STATE_UNAVAILABLE
        warnings = len(caplog.records)
        await update(hass, entry, humidifier, humidity)
        assert len(caplog.records) == warnings
    assert "secret-" not in caplog.text

    properties.update(current_humidity=49, device_status=2)
    get.side_effect = lambda _path: snapshot()
    await update(hass, entry, humidifier, humidity)
    assert hass.states.get(humidifier).state == "off"
    assert hass.states.get(humidifier).attributes["current_humidity"] == 49
    assert hass.states.get(humidity).state == "49"


async def test_missing_values_are_unknown_and_recover(hass, device):
    entry, properties, _, _, _, _, _, _ = device
    humidifier = entity_id(hass, "humidifier", "dehumidifier")
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    for name in ("device_mode", "device_status", "current_humidity"):
        properties.pop(name)
    await update(hass, entry, humidifier, humidity)
    assert hass.states.get(humidifier).state == STATE_UNKNOWN
    assert hass.states.get(humidity).state == STATE_UNKNOWN
    assert hass.states.get(humidifier).attributes.get("mode") is None
    properties.update(device_mode=2, device_status=1, current_humidity=52)
    await update(hass, entry, humidifier, humidity)
    assert hass.states.get(humidifier).state == "on"
    assert hass.states.get(humidity).state == "52"


async def test_partial_bad_snapshot_does_not_mutate_humidifier(hass, device, caplog):
    entry, properties, _, _, _, _, _, _ = device
    humidifier = entity_id(hass, "humidifier", "dehumidifier")
    entity = hass.data["humidifier"].get_entity(humidifier)
    old = (entity.mode, entity.target_humidity, entity.current_humidity, entity.is_on)
    properties.update(device_mode=2, humidity_setpoint="secret-bad-number")
    with caplog.at_level(logging.WARNING):
        await update(hass, entry, humidifier)
    assert hass.states.get(humidifier).state == STATE_UNAVAILABLE
    assert (
        entity.mode,
        entity.target_humidity,
        entity.current_humidity,
        entity.is_on,
    ) == old
    assert "secret-bad-number" not in caplog.text
    properties.update(humidity_setpoint=45, current_humidity=43, device_status=2)
    await update(hass, entry, humidifier)
    state = hass.states.get(humidifier)
    assert state.state == "off"
    assert state.attributes["mode"] == "DRY_CLOTHES"
    assert state.attributes["humidity"] == 45
    assert state.attributes["current_humidity"] == 43


@pytest.mark.parametrize("domain", ["sensor", "humidifier"])
async def test_cancellation_preserves_published_state(hass, device, domain):
    entry, _, snapshot, get, _, _, _, _ = device
    suffix = "dehumidifier" if domain == "humidifier" else "current_humidity_sensor"
    entity = entity_id(hass, domain, suffix)
    before = hass.states.get(entity).state
    started = asyncio.Event()

    async def slow(_path):
        started.set()
        await asyncio.Event().wait()
        return snapshot()

    get.side_effect = slow
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(async_update_entity(hass, entity))
    await asyncio.wait_for(started.wait(), 2)
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll
    assert hass.states.get(entity).state == before


@pytest.mark.parametrize("domain", ["sensor", "humidifier"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cancelled_poll_during_entity_startup_keeps_shared_result(
    hass, device, domain, fails
):
    """Real HA entity setup must retain its shared poll when a later poll cancels."""
    entry, properties, snapshot, get, _, _, _, _ = device
    suffix = "dehumidifier" if domain == "humidifier" else "current_humidity_sensor"
    source = hass.data[domain].get_entity(entity_id(hass, domain, suffix))
    client = entry.runtime_data
    if domain == "sensor":
        entity = GenericSensor(
            "STARTUP-DSN",
            source.device_info,
            client,
            "Current Humidity",
            client.get_current_humidity,
            SensorDeviceClass.HUMIDITY,
        )
    else:
        entity = DehumidifierEntity("STARTUP-DSN", source.device_info, client)
    properties.update(current_humidity=47, device_status=2)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        if fails:
            raise HomeAssistantError("temporary outage")
        return snapshot()

    async def wait_for_waiters():
        while client._properties_waiters < 2:
            await asyncio.sleep(0)

    get.side_effect = read
    get.reset_mock()
    client.device_properties_timestamp = 0
    setup = asyncio.create_task(source.platform.async_add_entities([entity]))
    poll = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        poll = asyncio.create_task(async_update_entity(hass, entity.entity_id))
        await asyncio.wait_for(wait_for_waiters(), 2)
        poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll
    finally:
        release.set()
        await setup
    expected = STATE_UNAVAILABLE if fails else ("47" if domain == "sensor" else "off")
    assert hass.states.get(entity.entity_id).state == expected
    assert get.await_count == 1


@pytest.mark.parametrize("domain", ["sensor", "humidifier"])
@pytest.mark.parametrize("later_fails", [False, True])
async def test_cancelled_poll_does_not_revoke_newer_completed_result(
    hass, device, domain, later_fails
):
    entry, properties, snapshot, _, _, _, _, _ = device
    suffix = "dehumidifier" if domain == "humidifier" else "current_humidity_sensor"
    entity_id_ = entity_id(hass, domain, suffix)
    entity = hass.data[domain].get_entity(entity_id_)
    client = entry.runtime_data
    old_snapshot = [item["property"] for item in snapshot()]
    properties.update(device_status=2, current_humidity=47)
    new_snapshot = [item["property"] for item in snapshot()]
    old_started, release_old, cancel_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    count = 0

    async def reads():
        nonlocal count
        count += 1
        if count == 1:
            old_started.set()
            await release_old.wait()
            return old_snapshot
        if count == 2:
            if later_fails:
                raise HomeAssistantError("outage")
            return new_snapshot
        cancel_started.set()
        await asyncio.Event().wait()

    with patch.object(client, "get_properties", AsyncMock(side_effect=reads)):
        old = asyncio.create_task(entity.async_update())
        await asyncio.wait_for(old_started.wait(), 2)
        try:
            await async_update_entity(hass, entity_id_)
            cancelled = asyncio.create_task(async_update_entity(hass, entity_id_))
            await asyncio.wait_for(cancel_started.wait(), 2)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
        finally:
            release_old.set()
            await old
        entity.async_write_ha_state()
    expected = (
        STATE_UNAVAILABLE if later_fails else ("47" if domain == "sensor" else "off")
    )
    assert hass.states.get(entity_id_).state == expected


@pytest.mark.parametrize("domain", ["sensor", "humidifier"])
async def test_cancelled_waiter_does_not_hide_auth_invalidation(hass, device, domain):
    entry, _, snapshot, get, _, _, _, _ = device
    suffix = "dehumidifier" if domain == "humidifier" else "current_humidity_sensor"
    entity_id_ = entity_id(hass, domain, suffix)
    entity = hass.data[domain].get_entity(entity_id_)
    client = entry.runtime_data
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    async def wait_for_waiters():
        while client._properties_waiters < 2:
            await asyncio.sleep(0)

    get.side_effect = read
    client.device_properties_timestamp = 0
    old = asyncio.create_task(entity.async_update())
    await asyncio.wait_for(started.wait(), 2)
    try:
        newer = asyncio.create_task(async_update_entity(hass, entity_id_))
        await asyncio.wait_for(wait_for_waiters(), 2)
        newer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await newer
        client.access_token = None
        with patch.object(
            client,
            "get_new_access_token",
            AsyncMock(side_effect=InvalidCredentialsError),
        ), pytest.raises(InvalidCredentialsError):
            await client.get_access_token()
        assert hass.states.get(entity_id_).state == STATE_UNAVAILABLE
    finally:
        release.set()
        await old
    entity.async_write_ha_state()
    assert hass.states.get(entity_id_).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("domain", ["sensor", "humidifier"])
@pytest.mark.parametrize("later_fails", [False, True])
@pytest.mark.parametrize("old_fails", [False, True])
async def test_old_callback_cannot_overwrite_newer_poll(
    hass, device, domain, later_fails, old_fails, caplog
):
    entry, properties, snapshot, _, _, _, _, _ = device
    suffix = "dehumidifier" if domain == "humidifier" else "current_humidity_sensor"
    entity_id_ = entity_id(hass, domain, suffix)
    entity = hass.data[domain].get_entity(entity_id_)
    client = entry.runtime_data
    old_snapshot = [item["property"] for item in snapshot()]
    properties.update(device_status=2, current_humidity=47)
    new_snapshot = [item["property"] for item in snapshot()]
    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def reads():
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await release.wait()
            if old_fails:
                raise HomeAssistantError("secret-old-failure")
            return old_snapshot
        if later_fails:
            raise HomeAssistantError("cloud failure")
        return new_snapshot

    with patch.object(client, "get_properties", AsyncMock(side_effect=reads)):
        old = asyncio.create_task(entity.async_update())
        await asyncio.wait_for(started.wait(), 2)
        try:
            await async_update_entity(hass, entity_id_)
        finally:
            release.set()
            await old
        entity.async_write_ha_state()
    expected = (
        STATE_UNAVAILABLE
        if later_fails
        else ("off" if domain == "humidifier" else "47")
    )
    assert hass.states.get(entity_id_).state == expected
    assert "secret-old-failure" not in caplog.text


async def test_confirmed_auth_failure_remains_unavailable(hass, device):
    entry, _, _, _, _, _, _, _ = device
    client = entry.runtime_data
    humidifier = entity_id(hass, "humidifier", "dehumidifier")
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    client.access_token = None
    with patch.object(
        client, "get_new_access_token", AsyncMock(side_effect=InvalidCredentialsError)
    ), pytest.raises(InvalidCredentialsError):
        await client.get_access_token()
    assert client.invalid_credentials
    await update(hass, entry, humidifier, humidity)
    assert hass.states.get(humidifier).state == STATE_UNAVAILABLE
    assert hass.states.get(humidity).state == STATE_UNAVAILABLE


async def test_auth_rejection_during_shared_poll_stays_unavailable(hass, device):
    entry, _, snapshot, get, _, _, _, _ = device
    client = entry.runtime_data
    humidifier = entity_id(hass, "humidifier", "dehumidifier")
    humidity = entity_id(hass, "sensor", "current_humidity_sensor")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = slow
    client.device_properties_timestamp = 0
    polls = [
        asyncio.create_task(async_update_entity(hass, item))
        for item in (humidifier, humidity)
    ]
    await asyncio.wait_for(started.wait(), 2)
    try:
        client.access_token = None
        with patch.object(
            client,
            "get_new_access_token",
            AsyncMock(side_effect=InvalidCredentialsError),
        ), pytest.raises(InvalidCredentialsError):
            await client.get_access_token()
        assert hass.states.get(humidifier).state == STATE_UNAVAILABLE
        assert hass.states.get(humidity).state == STATE_UNAVAILABLE
    finally:
        release.set()
        await asyncio.gather(*polls)
    assert hass.states.get(humidifier).state == STATE_UNAVAILABLE
    assert hass.states.get(humidity).state == STATE_UNAVAILABLE
