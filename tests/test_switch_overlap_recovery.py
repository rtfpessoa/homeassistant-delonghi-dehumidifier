"""Check switch recovery after overlapping commands through Home Assistant."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_switch_refresh
from tests.test_switch_refresh import refresh

device = test_switch_refresh.device


@pytest.mark.parametrize(
    ("property_name", "first_value", "second_value"),
    [("swing", 1, 0), ("set_eco", 0, 1)],
)
async def test_failed_overlapping_command_does_not_skip_next_scan(
    hass, device, property_name, first_value, second_value
):
    """A failed second command cannot suppress a later cloud refresh."""
    entry, properties, snapshot, get, post, eco, swing, eco_sensor = device
    switch = swing if property_name == "swing" else eco
    sensor = (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_swing_mode_sensor"
        )
        if property_name == "swing"
        else eco_sensor
    )
    properties[property_name] = second_value
    await refresh(hass, entry, switch, sensor)
    old_snapshot = snapshot()
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def delayed_read(_path):
        poll_started.set()
        await release_poll.wait()
        return old_snapshot

    async def write(_path, body):
        if body["datapoint"]["value"] == first_value:
            return {}
        write_started.set()
        await release_write.wait()
        raise HomeAssistantError("simulated write failure")

    get.side_effect = delayed_read
    post.side_effect = write
    entry.runtime_data.device_properties_timestamp = 0
    poll = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": switch}, blocking=True
        )
    )
    second_command = None
    try:
        await asyncio.wait_for(poll_started.wait(), 2)
        await hass.services.async_call(
            "switch",
            "turn_on" if first_value else "turn_off",
            {"entity_id": switch},
            blocking=True,
        )
        second_command = asyncio.create_task(
            hass.services.async_call(
                "switch",
                "turn_on" if second_value else "turn_off",
                {"entity_id": switch},
                blocking=True,
            )
        )
        await asyncio.wait_for(write_started.wait(), 2)
        release_poll.set()
        await poll
        release_write.set()
        with pytest.raises(HomeAssistantError):
            await second_command
    finally:
        release_poll.set()
        release_write.set()
        await poll
        if second_command is not None and not second_command.done():
            try:
                await second_command
            except HomeAssistantError:
                pass

    async def fresh_read(_path):
        return snapshot()

    get.side_effect = fresh_read
    await refresh(hass, entry, sensor)
    assert hass.states.get(sensor).state == ("ON" if second_value else "OFF")
    assert hass.states.get(switch).state == ("on" if first_value else "off")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == ("on" if second_value else "off")
