"""Verify an overlapping rejected command still starts reauthentication."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_runtime_reauth

loaded_entry = test_runtime_reauth.loaded_entry


async def test_overlapping_rejected_command_marks_switch_unavailable(
    hass, loaded_entry
):
    entry, session, sensor_ids = loaded_entry
    client = entry.runtime_data
    switch = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_test-dsn_swing_mode_switch"
    )
    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    session.pause = True
    original_post = client.post_request

    async def delayed_read(_path):
        poll_started.set()
        await release_poll.wait()
        return [{"property": prop} for prop in test_runtime_reauth.PROPERTIES]

    async def write(path, body):
        if body["datapoint"]["value"] == 1:
            return {}
        return await original_post(path, body)

    with (
        patch.object(client, "get_request", side_effect=delayed_read),
        patch.object(client, "post_request", side_effect=write),
    ):
        client.device_properties_timestamp = 0
        poll = asyncio.create_task(async_update_entity(hass, switch))
        second_command = None
        try:
            await asyncio.wait_for(poll_started.wait(), 2)
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch}, blocking=True
            )
            second_command = asyncio.create_task(
                hass.services.async_call(
                    "switch", "turn_off", {"entity_id": switch}, blocking=True
                )
            )
            await asyncio.wait_for(session.login_started.wait(), 2)
            release_poll.set()
            await poll
            session.continue_login.set()
            with pytest.raises(InvalidCredentialsError):
                await second_command
        finally:
            release_poll.set()
            session.continue_login.set()
            await poll
            if second_command is not None and not second_command.done():
                try:
                    await second_command
                except InvalidCredentialsError:
                    pass
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "unavailable"
    assert all(hass.states.get(entity).state == "unavailable" for entity in sensor_ids)
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
