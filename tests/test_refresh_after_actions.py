"""Test post-action observations through Home Assistant services and states."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.humidifier import DATA_COMPONENT as HUMIDIFIER_COMPONENT
from homeassistant.components.switch import DATA_COMPONENT as SWITCH_COMPONENT
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    EVENT_LOGBOOK_ENTRY,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api import action_refresh, humidifier
from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests import (
    real_feel_helpers,
    test_humidifier_sync,
    test_real_feel_mode,
    test_runtime_reauth,
    test_switch_refresh,
)

device = test_switch_refresh.device
cloud = real_feel_helpers.cloud
loaded_entry = test_runtime_reauth.loaded_entry


def find_entity(hass, domain, suffix):
    """Find an integration entity without relying on its suggested name."""
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{DOMAIN}_TEST-DSN_{suffix}"
    )


async def wait_for_state(hass, entity_id, expected):
    """Wait for a real HA state event, including one already emitted."""
    if hass.states.get(entity_id).state == expected:
        return
    done = asyncio.Event()

    @callback
    def on_change(event):
        if event.data["new_state"].state == expected:
            done.set()

    unsubscribe = async_track_state_change_event(hass, [entity_id], on_change)
    try:
        await asyncio.wait_for(done.wait(), timeout=3)
    finally:
        unsubscribe()


async def test_switch_action_refreshes_other_entities(hass, device):
    """A delayed cloud observation reaches all enabled entity platforms."""
    _, properties, _, get, _, eco, _, eco_sensor = device
    registry = er.async_get(hass)
    humidity = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_current_humidity_sensor"
    )
    alarm = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_device_alarm_binary_sensor"
    )
    changed = asyncio.Event()

    @callback
    def handle_change(_event):
        changed.set()

    unsubscribe = async_track_state_change_event(hass, [eco_sensor], handle_change)
    try:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        reads = get.await_count
        properties.update(set_eco=1, current_humidity=59, alarm_state=3)
        await asyncio.wait_for(changed.wait(), timeout=5.5)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(eco_sensor).state == "ON"
        assert hass.states.get(humidity).state == "59"
        assert hass.states.get(alarm).state == "on"
        assert get.await_count == reads + 1
    finally:
        unsubscribe()


@pytest.mark.parametrize(
    ("domain", "service", "target", "data"),
    [
        ("humidifier", "turn_on", "humidifier", {}),
        ("humidifier", "turn_off", "humidifier", {}),
        ("humidifier", "set_humidity", "humidifier", {"humidity": 45}),
        ("humidifier", "set_mode", "humidifier", {"mode": "DRY_CLOTHES"}),
        ("switch", "turn_on", "eco", {}),
        ("switch", "turn_on", "swing", {}),
        ("switch", "turn_off", "eco", {}),
        ("switch", "turn_off", "swing", {}),
    ],
)
async def test_each_action_refreshes_all_platforms(
    hass, device, domain, service, target, data
):
    """All controls refresh other enabled entities using one shared observation."""
    _, properties, _, get, post, eco, swing, eco_sensor = device
    humidifier = test_humidifier_sync.humidifier_id(hass)
    humidity = find_entity(hass, "sensor", "current_humidity_sensor")
    alarm = find_entity(hass, "binary_sensor", "device_alarm_binary_sensor")
    entity_id = {"humidifier": humidifier, "eco": eco, "swing": swing}[target]
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )
        reads = get.await_count
        properties.update(set_eco=1, swing=1, current_humidity=59, alarm_state=3)
        await wait_for_state(hass, humidity, "59")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(eco_sensor).state == "ON"
    assert hass.states.get(eco).state == "on"
    assert hass.states.get(swing).state == "on"
    assert hass.states.get(alarm).state == "on"
    assert hass.states.get(humidifier).attributes["current_humidity"] == 59
    assert get.await_count == reads + 1
    post.assert_awaited_once()


async def test_old_staged_polls_do_not_lose_followup(hass, device):
    """A delayed request survives HA's suppression of overlapping entity updates."""
    entry, properties, snapshot, get, _, eco, _, eco_sensor = device
    humidity = find_entity(hass, "sensor", "current_humidity_sensor")
    alarm = find_entity(hass, "binary_sensor", "device_alarm_binary_sensor")
    humidifier = test_humidifier_sync.humidifier_id(hass)
    old = snapshot()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old
        return snapshot()

    get.side_effect = read
    entry.runtime_data.invalidate_properties()
    polls = [
        asyncio.create_task(async_update_entity(hass, entity))
        for entity in (humidity, alarm, humidifier, eco, eco_sensor)
    ]
    try:
        await asyncio.wait_for(started.wait(), 2)
        with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
            properties.update(set_eco=1, current_humidity=59, alarm_state=3)
            await asyncio.sleep(0.1)
            assert calls == 1
            release.set()
            await asyncio.gather(*polls)
            await wait_for_state(hass, humidity, "59")
            await hass.async_block_till_done(wait_background_tasks=True)
        assert calls == 2
        assert hass.states.get(eco).state == "on"
        assert hass.states.get(eco_sensor).state == "ON"
        assert hass.states.get(alarm).state == "on"
        assert hass.states.get(humidifier).attributes["current_humidity"] == 59
    finally:
        release.set()
        await asyncio.gather(*polls, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_ambiguous_write_still_refreshes_without_retry(hass, device, outcome):
    """A write may reach the device even when its caller receives no response."""
    _, properties, _, get, post, eco, _, eco_sensor = device
    started = asyncio.Event()

    async def write(_path, _body):
        properties["set_eco"] = 1
        started.set()
        if outcome == "timeout":
            raise TimeoutError
        await asyncio.Event().wait()

    post.side_effect = write
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        command = asyncio.create_task(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        if outcome == "cancel":
            command.cancel()
        with pytest.raises((TimeoutError, asyncio.CancelledError)):
            await command
        reads = get.await_count
        await wait_for_state(hass, eco_sensor, "ON")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(eco).state == "on"
    assert get.await_count == reads + 1
    post.assert_awaited_once()


async def test_multiple_actions_coalesce_after_last_write(hass, device):
    """A later in-flight command postpones the batch until it has completed."""
    _, properties, _, get, post, eco, swing, eco_sensor = device
    started, release = asyncio.Event(), asyncio.Event()

    async def write(path, _body):
        if "/swing/" in path:
            started.set()
            await release.wait()
        return {}

    post.side_effect = write
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.08):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        reads = get.await_count
        second = asyncio.create_task(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": swing}, blocking=True
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            properties.update(set_eco=1, swing=1)
            await asyncio.sleep(0.15)
            assert get.await_count == reads
        finally:
            release.set()
            await second
        await wait_for_state(hass, eco_sensor, "ON")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert get.await_count == reads + 1
    assert post.await_count == 2


async def test_newer_action_supersedes_an_inflight_action_refresh(hass, device):
    """A follow-up read begun before another write cannot win over the later read."""
    _, properties, snapshot, get, _, eco, swing, eco_sensor = device
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        result = snapshot()
        if calls == 1:
            started.set()
            await release.wait()
        return result

    get.side_effect = read
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": swing}, blocking=True
            )
            properties.update(set_eco=1, swing=1)
            await asyncio.sleep(0.1)
        finally:
            release.set()
        await wait_for_state(hass, eco_sensor, "ON")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == 2
    assert hass.states.get(eco).state == "on"
    assert hass.states.get(swing).state == "on"


async def test_unload_cancels_pending_refresh(hass, device):
    """An unloaded entry does not poll on a timer from its old client."""
    entry, _, _, get, _, eco, *_ = device
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        reads = get.await_count
        assert await hass.config_entries.async_unload(entry.entry_id)
        await asyncio.sleep(0.12)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert get.await_count == reads


async def test_unload_during_followup_cancels_the_shared_read(hass, device):
    """Unload does not leave an in-flight observation or another delayed poll."""
    entry, _, _, get, post, eco, *_ = device
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    get.side_effect = read
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        await asyncio.wait_for(started.wait(), 2)
        reads = get.await_count
        assert await asyncio.wait_for(
            hass.config_entries.async_unload(entry.entry_id), 2
        )
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.sleep(0.12)
    assert get.await_count == reads
    post.assert_awaited_once()


async def test_followup_is_scoped_to_its_config_entry(hass, device):
    """A command does not force updates in another integration entry."""
    _, properties, _, _, _, eco, _, _ = device
    other = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "other@example.invalid",
            CONF_PASSWORD: "test",
            CONF_LANGUAGE: "en",
        },
    )
    other.add_to_hass(hass)
    with patch.object(APIClient, "get_first_device", return_value="OTHER-DSN"):
        assert await hass.config_entries.async_setup(other.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    first_humidity = find_entity(hass, "sensor", "current_humidity_sensor")
    other_humidity = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_OTHER-DSN_current_humidity_sensor"
    )
    try:
        with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
            properties["current_humidity"] = 59
            await wait_for_state(hass, first_humidity, "59")
            await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(other_humidity).state == "54"
    finally:
        await hass.config_entries.async_unload(other.entry_id)


async def test_disabled_entity_is_not_refreshed(hass, device):
    """Only entities added to HA subscribe to the follow-up observation."""
    _, properties, _, _, _, eco, _, eco_sensor = device
    registry = er.async_get(hass)
    registry.async_update_entity(eco_sensor, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        properties.update(set_eco=1, current_humidity=59)
        humidity = find_entity(hass, "sensor", "current_humidity_sensor")
        await wait_for_state(hass, humidity, "59")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(eco_sensor) is None
    assert hass.states.get(eco).state == "on"


async def test_renamed_sensor_receives_action_refresh(hass, device):
    """HA re-adds the same entity instance when an entity ID is renamed."""
    _, properties, _, _, _, eco, _, old_id = device
    new_id = "sensor.renamed_eco_mode"
    er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        properties.update(set_eco=1, current_humidity=59)
        await wait_for_state(
            hass, find_entity(hass, "sensor", "current_humidity_sensor"), "59"
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(new_id).state == "ON"


async def test_renamed_humidifier_accepts_commands_and_action_refresh(hass, device):
    """A renamed humidifier remains controllable and receives the delayed read."""
    _, properties, _, _, post, *_ = device
    old_id = test_humidifier_sync.humidifier_id(hass)
    new_id = "humidifier.renamed_dehumidifier"
    er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": new_id}, blocking=True
        )
        properties.update(device_status=0, current_humidity=59)
        await wait_for_state(
            hass, find_entity(hass, "sensor", "current_humidity_sensor"), "59"
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(new_id).attributes["current_humidity"] == 59
    post.assert_awaited_once()


async def test_renamed_humidifier_accepts_real_feel_exit(hass, cloud):
    """The mode guard can run after HA removes and re-adds this entity."""
    await real_feel_helpers.setup(hass, cloud)
    old_id = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    new_id = "humidifier.renamed_dehumidifier"
    er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(
        "humidifier",
        "set_mode",
        {"entity_id": new_id, "mode": "DEHUMIDIFY"},
        blocking=True,
    )
    cloud[4].assert_awaited_once()


async def test_old_exit_guard_cannot_write_after_entity_rename(hass, cloud):
    """Re-adding the entity must not revive a preflight from its old lifetime."""
    await real_feel_helpers.setup(hass, cloud)
    _, _, snapshot, get, post = cloud
    old_id = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    new_id = "humidifier.renamed_dehumidifier"
    humidifier = hass.data[HUMIDIFIER_COMPONENT].get_entity(old_id)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    task = asyncio.create_task(
        hass.services.async_call(
            "humidifier",
            "set_mode",
            {"entity_id": old_id, "mode": "DEHUMIDIFY"},
            blocking=True,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)

        async def wait_for_readd():
            while humidifier.entity_id != new_id or humidifier._removed:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_readd(), 3)
        release.set()
        with pytest.raises((ServiceValidationError, asyncio.CancelledError)):
            await task
        post.assert_not_awaited()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_old_mode_write_does_not_restore_pending_timer_after_rename(hass, cloud):
    """A POST from an old entity lifetime cannot create a timer after re-add."""
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await real_feel_helpers.setup(hass, cloud)
    old_id = test_real_feel_mode.humidifier_id(hass)
    new_id = "humidifier.renamed_dehumidifier"
    entity = hass.data[HUMIDIFIER_COMPONENT].get_entity(old_id)
    started, release = asyncio.Event(), asyncio.Event()

    async def write(_path, _body, **_kwargs):
        started.set()
        await release.wait()
        return {}

    cloud[4].side_effect = write
    task = asyncio.create_task(test_real_feel_mode.set_mode(hass, "REAL_FEEL"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)

        async def wait_for_readd():
            while entity.entity_id != new_id or entity._removed:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_readd(), 3)
        release.set()
        await task
        assert entity._pending_mode is None
        assert entity._pending_timer is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_newer_completed_write_supersedes_queued_notification(hass, device):
    """A callback from an earlier timer must not bypass a newer switch command."""
    entry, properties, _, get, post, eco, *_ = device
    switch = hass.data[SWITCH_COMPONENT].get_entity(eco)
    second_done = asyncio.Event()
    unsubscribe = None

    @callback
    def on_refresh(_generation):
        nonlocal unsubscribe
        unsubscribe()
        with patch.object(action_refresh, "_REFRESH_DELAY", 3600):
            command = asyncio.Task(
                switch.async_turn_off(), loop=hass.loop, eager_start=True
            )
        assert command.done()
        command.result()
        switch.async_write_ha_state()
        second_done.set()

    with patch.object(action_refresh, "_REFRESH_DELAY", 0.15):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        reads = get.await_count
        properties["set_eco"] = 1
        unsubscribe = entry.runtime_data.action_refresh.subscribe(on_refresh)
        await asyncio.wait_for(second_done.wait(), 2)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(eco).state == "off"
    assert get.await_count == reads
    assert post.await_count == 2


async def test_auth_rejection_stops_followup(hass, loaded_entry):
    """Rejected credentials do not schedule additional cloud traffic or reauths."""
    _, session, _ = loaded_entry
    registry = er.async_get(hass)
    eco = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_test-dsn_eco_mode_switch"
    )
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": eco}, blocking=True
            )
        await asyncio.sleep(0.12)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    assert hass.states.get(eco).state == "unavailable"


async def test_failed_followup_marks_entities_unavailable_and_recovers(hass, device):
    """A single failed observation affects all entities and later polling recovers."""
    entry, properties, snapshot, get, post, eco, _, eco_sensor = device
    alarm = find_entity(hass, "binary_sensor", "device_alarm_binary_sensor")
    humidifier = test_humidifier_sync.humidifier_id(hass)
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
        reads = get.await_count
        get.side_effect = HomeAssistantError("temporary connection error")
        await wait_for_state(hass, eco_sensor, "unavailable")
        await hass.async_block_till_done(wait_background_tasks=True)
        assert get.await_count == reads + 1
        assert all(
            hass.states.get(entity).state == "unavailable"
            for entity in (eco, eco_sensor, alarm, humidifier)
        )
        properties.update(set_eco=1, alarm_state=3)
        get.side_effect = lambda _path: snapshot()
        await test_switch_refresh.refresh(
            hass, entry, eco, eco_sensor, alarm, humidifier
        )
    assert hass.states.get(eco_sensor).state == "ON"
    assert hass.states.get(alarm).state == "on"
    post.assert_awaited_once()


async def test_invalid_mode_does_not_schedule_followup(hass, device):
    """Input rejected before a write does not cause a delayed GET."""
    _, _, _, get, post, *_ = device
    humidifier = test_humidifier_sync.humidifier_id(hass)
    reads = get.await_count
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "humidifier",
                "set_mode",
                {"entity_id": humidifier, "mode": "invalid"},
                blocking=True,
            )
        await asyncio.sleep(0.12)
    assert get.await_count == reads
    post.assert_not_awaited()


@pytest.mark.parametrize("mode", ["REAL_FEEL", "DEHUMIDIFY"])
async def test_tested_real_feel_actions_refresh_all_entities(hass, cloud, mode):
    """The guarded mode path also schedules the shared observation."""
    await real_feel_helpers.setup(hass, cloud)
    _, properties, _, get, post = cloud
    registry = er.async_get(hass)
    humidifier = registry.async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    humidity = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_test-dsn_current_humidity_sensor"
    )
    with patch.object(action_refresh, "_REFRESH_DELAY", 0.05):
        await hass.services.async_call(
            "humidifier",
            "set_mode",
            {"entity_id": humidifier, "mode": mode},
            blocking=True,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        reads = get.await_count
        properties["current_humidity"] = 57
        await wait_for_state(hass, humidity, "57")
        await hass.async_block_till_done(wait_background_tasks=True)
    assert get.await_count == reads + 1
    assert post.await_count == 1


@pytest.mark.parametrize(
    ("initial", "requested", "confirmed", "message"),
    [
        (
            {"device_mode": 2, "activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "REAL_FEEL",
            {"device_mode": 1, "activate_realfeel": REAL_FEEL_ACTIVATION_PAYLOAD},
            "Reported mode: Real Feel",
        ),
        (
            {"device_mode": 1, "activate_realfeel": REAL_FEEL_ACTIVATION_PAYLOAD},
            "DEHUMIDIFY",
            {"device_mode": 1, "activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "Reported mode: Dehumidify",
        ),
    ],
)
async def test_delayed_stale_mode_echo_does_not_create_activity(
    hass, cloud, initial, requested, confirmed, message
):
    """The delayed read must not treat the old mode as an acknowledged command."""
    _, properties, snapshot, get, post = cloud
    properties.update(initial)
    events = []
    fetched = asyncio.Event()

    @callback
    def on_entry(event):
        events.append(event.data)

    hass.bus.async_listen(EVENT_LOGBOOK_ENTRY, on_entry)
    await real_feel_helpers.setup(hass, cloud)
    entity_id = test_real_feel_mode.humidifier_id(hass)
    with (
        patch.object(action_refresh, "_REFRESH_DELAY", 0.05),
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 100),
    ):
        await test_real_feel_mode.set_mode(hass, requested)

        async def read(_path):
            fetched.set()
            return snapshot()

        get.side_effect = read
        await asyncio.wait_for(fetched.wait(), 3)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(entity_id).attributes["mode"] is None
        assert events == []
        properties.update(confirmed)
        await test_real_feel_mode.refresh(hass, cloud)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entity_id).attributes["mode"] == requested
    assert [event["message"] for event in events] == [message]
    post.assert_awaited_once()
