"""Test mode activity through Home Assistant's recorder and logbook."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.logbook import async_log_entry
from homeassistant.components.logbook.helpers import async_determine_event_types
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.const import EVENT_LOGBOOK_ENTRY
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.delonghi_dehumidifier_api import humidifier
from custom_components.delonghi_dehumidifier_api.const import (
    REAL_FEEL_ACTIVATION_PAYLOAD,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests import real_feel_helpers, test_real_feel_mode

cloud = real_feel_helpers.cloud


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Prepare the Recorder database before the shared HA fixture starts."""
    yield


async def activity_entries(hass, recorder, entity_id):
    """Query exactly the device-page combination of entity and device filters."""
    await hass.async_block_till_done(wait_background_tasks=True)
    await recorder.async_block_till_done()
    device_id = er.async_get(hass).async_get(entity_id).device_id
    entities = [
        entry.entity_id
        for entry in er.async_entries_for_device(er.async_get(hass), device_id)
    ]
    devices = [device_id]
    processor = EventProcessor(
        hass,
        async_determine_event_types(hass, entities, devices),
        entities,
        devices,
    )
    now = dt_util.utcnow()
    return await hass.async_add_executor_job(
        processor.get_events, now - timedelta(minutes=1), now + timedelta(minutes=1)
    )


async def test_entity_linked_control_appears_in_device_activity(
    recorder_mock, hass, cloud
):
    """Verify the real device-page query recognizes an entity-linked event."""
    assert await async_setup_component(hass, "logbook", {})
    await real_feel_helpers.setup(hass, cloud)
    entity_id = test_real_feel_mode.humidifier_id(hass)
    async_log_entry(hass, "Test", "Control event", entity_id=entity_id)
    entries = await activity_entries(hass, recorder_mock, entity_id)
    assert any(event.get("message") == "Control event" for event in entries)


async def test_confirmed_mode_change_appears_in_device_activity(
    recorder_mock, hass, cloud
):
    """A successful selection that is observed in the cloud is visible in Activity."""
    assert await async_setup_component(hass, "logbook", {})
    cloud[1].update(device_mode=2, activate_realfeel=REAL_FEEL_DEACTIVATION_PAYLOAD)
    await real_feel_helpers.setup(hass, cloud)
    entity_id = test_real_feel_mode.humidifier_id(hass)
    assert hass.states.get(entity_id).attributes["mode"] == "DRY_CLOTHES"

    async def write(_path, _body, **_kwargs):
        cloud[1].update(device_mode=1, activate_realfeel=REAL_FEEL_ACTIVATION_PAYLOAD)
        return {}

    cloud[4].side_effect = write
    await test_real_feel_mode.set_mode(hass, "REAL_FEEL")
    assert hass.states.get(entity_id).attributes["mode"] == "REAL_FEEL"
    sensor_id = er.async_get(hass).async_get_entity_id(
        "sensor",
        "delonghi_dehumidifier_api",
        "delonghi_dehumidifier_api_test-dsn_device_mode_sensor",
    )
    await async_update_entity(hass, sensor_id)
    entries = await activity_entries(hass, recorder_mock, entity_id)
    assert any(event.get("message") == "Reported mode: Real Feel" for event in entries)
    assert any(
        event.get("entity_id") == sensor_id and event.get("state") == "DEHUMIDIFY"
        for event in entries
    )


@pytest.mark.parametrize(
    ("language", "reported"),
    [
        ("en", "Reported mode: Real Feel"),
        ("de", "Gemeldeter Modus: Real Feel"),
        ("fr", "Reported mode: Real Feel"),
    ],
)
@pytest.mark.parametrize("source", [2, 3])
async def test_external_change_is_reported_once(
    hass, cloud, language, reported, source
):
    """Readback changes, including those from the app, use the server language."""
    hass.config.language = language
    cloud[1].update(device_mode=source)
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    assert "logbook" not in hass.config.components
    cloud[1].update(device_mode=1, activate_realfeel=REAL_FEEL_ACTIVATION_PAYLOAD)
    await test_real_feel_mode.refresh(hass, cloud)
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == [reported]
    assert events[0]["entity_id"] == test_real_feel_mode.humidifier_id(hass)


@pytest.mark.parametrize(
    ("language", "cloud_update", "expected"),
    [
        (
            "en",
            {"activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "Reported mode: Dehumidify",
        ),
        (
            "de",
            {"activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "Gemeldeter Modus: Entfeuchten",
        ),
        ("de", {"device_mode": 2}, "Gemeldeter Modus: Wäsche trocknen"),
        ("de", {"device_mode": 3}, "Gemeldeter Modus: Luftreinigung"),
    ],
)
async def test_real_feel_exit_is_reported(
    hass, cloud, language, cloud_update, expected
):
    hass.config.language = language
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    cloud[1].update(cloud_update)
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == [expected]


async def test_unknown_pending_and_same_mode_do_not_create_events(hass, cloud):
    """Unknown snapshots and stale command echoes do not become activity."""
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    await test_real_feel_mode.set_mode(hass, "REAL_FEEL")
    await hass.async_block_till_done()
    assert (
        hass.states.get(test_real_feel_mode.humidifier_id(hass)).attributes["mode"]
        is None
    )
    assert events == []
    cloud[1]["activate_realfeel"] = "unknown"
    await test_real_feel_mode.refresh(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    await test_real_feel_mode.refresh(hass, cloud)
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == ["Reported mode: Real Feel"]


async def test_ambiguous_failed_command_does_not_log_an_attempt(hass, cloud):
    """An unsuccessful write is not reported unless a later read confirms it."""
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    cloud[4].side_effect = TimeoutError("lost response")
    with pytest.raises(HomeAssistantError):
        await test_real_feel_mode.set_mode(hass, "REAL_FEEL")
    await hass.async_block_till_done()
    assert events == []
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == ["Reported mode: Real Feel"]


async def test_ordinary_mode_changes_do_not_add_duplicate_activity(hass, cloud):
    """The existing raw mode sensor continues to represent ordinary modes."""
    cloud[1]["device_mode"] = 2
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    cloud[1]["device_mode"] = 3
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert events == []


async def test_scheduled_followup_reports_the_observed_mode(hass, cloud):
    """The bounded retry added by the refresh fix can produce the activity event."""
    cloud[1]["device_mode"] = 2
    events = []
    reported = asyncio.Event()

    @callback
    def on_entry(event):
        events.append(event.data)
        reported.set()

    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, on_entry)
    await real_feel_helpers.setup(hass, cloud)
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await test_real_feel_mode.set_mode(hass, "REAL_FEEL")
        assert events == []
        cloud[1].update(device_mode=1, activate_realfeel=REAL_FEEL_ACTIVATION_PAYLOAD)
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.wait_for(reported.wait(), 3)
        assert [event["message"] for event in events] == ["Reported mode: Real Feel"]


async def test_unavailable_recovery_keeps_the_previous_baseline(hass, cloud):
    """A transient fetch failure and recovery at the same mode do not log."""
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    cloud[3].side_effect = TimeoutError("test")
    await test_real_feel_mode.refresh(hass, cloud)
    assert (
        hass.states.get(test_real_feel_mode.humidifier_id(hass)).state == "unavailable"
    )
    cloud[3].side_effect = lambda _: cloud[2]()
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert events == []
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == ["Reported mode: Dehumidify"]


@pytest.mark.parametrize("cloud", [{"appliance_model": "other"}], indirect=True)
async def test_unadvertised_real_feel_does_not_log(hass, cloud):
    """A legacy raw 100 on an unsupported model is not a verified mode."""
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    cloud[1]["device_mode"] = 100
    await test_real_feel_mode.refresh(hass, cloud)
    cloud[1]["device_mode"] = 2
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert events == []


async def test_reload_establishes_a_new_baseline(hass, cloud):
    """The first observation after unload/reload is not a new mode change."""
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    assert await hass.config_entries.async_unload(cloud[0].entry_id)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await real_feel_helpers.setup(hass, cloud)
    await hass.async_block_till_done()
    assert events == []
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    await test_real_feel_mode.refresh(hass, cloud)
    await hass.async_block_till_done()
    assert [event["message"] for event in events] == ["Reported mode: Real Feel"]


async def test_older_poll_cannot_create_an_activity_event(hass, cloud):
    """An outdated Real Feel response cannot overtake a newer accepted poll."""
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    entity = hass.data["humidifier"].get_entity(test_real_feel_mode.humidifier_id(hass))
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    old = [item["property"] for item in cloud[2]()]
    cloud[1]["device_mode"] = 2
    new = [item["property"] for item in cloud[2]()]
    started, release = asyncio.Event(), asyncio.Event()

    async def read():
        if not started.is_set():
            started.set()
            await release.wait()
            return old
        return new

    with patch.object(cloud[0].runtime_data, "get_properties", side_effect=read):
        older = asyncio.create_task(entity.async_update())
        await started.wait()
        await entity.async_update()
        release.set()
        await older
    await hass.async_block_till_done()
    assert entity.mode == "DRY_CLOTHES"
    assert events == []


async def test_unload_suppresses_a_late_poll_event(hass, cloud):
    """A late completion after removing the entity cannot create activity."""
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    events = []
    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, lambda event: events.append(event.data))
    await real_feel_helpers.setup(hass, cloud)
    entity = hass.data["humidifier"].get_entity(test_real_feel_mode.humidifier_id(hass))
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    snapshot = [item["property"] for item in cloud[2]()]
    started, release = asyncio.Event(), asyncio.Event()

    async def read():
        started.set()
        await release.wait()
        return snapshot

    with patch.object(cloud[0].runtime_data, "get_properties", side_effect=read):
        pending = asyncio.create_task(entity.async_update())
        await started.wait()
        assert await hass.config_entries.async_unload(cloud[0].entry_id)
        release.set()
        await pending
    await hass.async_block_till_done()
    assert events == []


async def test_device_page_live_logbook_stream(
    recorder_mock, hass, hass_ws_client, cloud
):
    """The actual filtered device Activity websocket receives a live event."""
    assert await async_setup_component(hass, "logbook", {})
    cloud[1]["device_mode"] = 2
    await real_feel_helpers.setup(hass, cloud)
    registry = er.async_get(hass)
    entity_id = test_real_feel_mode.humidifier_id(hass)
    device_id = registry.async_get(entity_id).device_id
    entity_ids = [
        entry.entity_id for entry in er.async_entries_for_device(registry, device_id)
    ]
    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "logbook/event_stream",
            "start_time": (dt_util.utcnow() - timedelta(minutes=1)).isoformat(),
            "entity_ids": entity_ids,
            "device_ids": [device_id],
        }
    )
    assert (await client.receive_json())["success"]
    while (await asyncio.wait_for(client.receive_json(), 5))["event"].get("partial"):
        pass
    cloud[1].update(device_mode=1, activate_realfeel=REAL_FEEL_ACTIVATION_PAYLOAD)
    await test_real_feel_mode.refresh(hass, cloud)
    message = await asyncio.wait_for(client.receive_json(), 5)
    assert any(
        event.get("message") == "Reported mode: Real Feel"
        and event.get("entity_id") == entity_id
        for event in message["event"]["events"]
    )
