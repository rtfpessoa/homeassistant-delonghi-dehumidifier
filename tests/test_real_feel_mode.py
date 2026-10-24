"""Exercise the best-effort Real Feel mode through Home Assistant."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api import action_refresh, humidifier
from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests import real_feel_helpers
from tests.test_client_http import make_client

cloud = real_feel_helpers.cloud
setup = real_feel_helpers.setup


def humidifier_id(hass):
    return er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )


async def set_mode(hass, mode):
    await hass.services.async_call(
        "humidifier",
        "set_mode",
        {"entity_id": humidifier_id(hass), "mode": mode},
        blocking=True,
    )


async def refresh(hass, cloud):
    cloud[0].runtime_data.invalidate_properties()
    await async_update_entity(hass, humidifier_id(hass))


@pytest.mark.parametrize(
    ("cloud", "expected_modes", "expected_mode"),
    [
        ({}, ["DEHUMIDIFY", "DRY_CLOTHES", "PURIFIER", "REAL_FEEL"], "REAL_FEEL"),
        (
            {"firmware_version": 1.0},
            ["DEHUMIDIFY", "DRY_CLOTHES", "PURIFIER", "REAL_FEEL"],
            "REAL_FEEL",
        ),
        (
            {"firmware_version": "1"},
            ["DEHUMIDIFY", "DRY_CLOTHES", "PURIFIER"],
            "DEHUMIDIFY",
        ),
        (
            {"firmware_version": True},
            ["DEHUMIDIFY", "DRY_CLOTHES", "PURIFIER"],
            "DEHUMIDIFY",
        ),
        (
            {"appliance_model": "other"},
            ["DEHUMIDIFY", "DRY_CLOTHES", "PURIFIER"],
            "DEHUMIDIFY",
        ),
    ],
    indirect=["cloud"],
)
async def test_mode_capability_is_limited_to_tested_identity(
    hass, cloud, expected_modes, expected_mode
):
    """The existing entity is the selector and setup never sends a write."""
    await setup(hass, cloud)
    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["available_modes"] == expected_modes
    assert state.attributes["mode"] == expected_mode
    assert state.attributes["humidity"] == 55
    cloud[4].assert_not_awaited()


@pytest.mark.parametrize(
    ("device_mode", "command", "expected"),
    [
        (1, REAL_FEEL_ACTIVATION_PAYLOAD, "REAL_FEEL"),
        (1, REAL_FEEL_DEACTIVATION_PAYLOAD, "DEHUMIDIFY"),
        (2, REAL_FEEL_ACTIVATION_PAYLOAD, "DRY_CLOTHES"),
        (3, REAL_FEEL_ACTIVATION_PAYLOAD, "PURIFIER"),
        (1, "other", None),
        (1, None, None),
        (1, True, None),
        ("1", REAL_FEEL_ACTIVATION_PAYLOAD, None),
        (1.0, REAL_FEEL_ACTIVATION_PAYLOAD, None),
        (100, REAL_FEEL_ACTIVATION_PAYLOAD, None),
    ],
)
async def test_snapshot_mode_mapping(hass, cloud, device_mode, command, expected):
    """Only exact known echoes are inferred; ordinary modes take precedence."""
    await setup(hass, cloud)
    cloud[1].update(device_mode=device_mode, activate_realfeel=command)
    await refresh(hass, cloud)
    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["mode"] == expected
    assert state.attributes["current_humidity"] == 50


@pytest.mark.parametrize("invalid", [True, False, 1.5, "secret-bad-mode"])
async def test_invalid_tested_mode_rejects_whole_snapshot_and_recovers(
    hass, cloud, caplog, invalid
):
    """A bad tested-device mode cannot publish the other snapshot values."""
    await setup(hass, cloud)
    entity_id = humidifier_id(hass)
    entity = hass.data["humidifier"].get_entity(entity_id)
    assert hass.states.get(entity_id).attributes["mode"] == "REAL_FEEL"
    previous = (
        entity.mode,
        entity.target_humidity,
        entity.current_humidity,
        entity.is_on,
    )
    cloud[1].update(
        device_mode=invalid, humidity_setpoint=45, current_humidity=43, device_status=2
    )
    with caplog.at_level(logging.DEBUG):
        await refresh(hass, cloud)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert (
        entity.mode,
        entity.target_humidity,
        entity.current_humidity,
        entity.is_on,
    ) == previous
    assert "secret-bad-mode" not in caplog.text
    cloud[1]["device_mode"] = 2
    await refresh(hass, cloud)
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["mode"] == "DRY_CLOTHES"
    assert state.attributes["humidity"] == 45
    assert state.attributes["current_humidity"] == 43


async def test_invalid_duplicate_tested_mode_rejects_snapshot(hass, cloud):
    """A malformed duplicate cannot hide behind strict ambiguous-mode detection."""
    await setup(hass, cloud)
    records = cloud[2]()
    records.append({"property": {"name": "device_mode", "value": True}})
    cloud[3].side_effect = None
    cloud[3].return_value = records
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("cloud", [{"set_eco": True}], indirect=True)
async def test_malformed_switch_value_during_setup_recovers(hass, cloud):
    """An invalid startup value creates an unavailable switch, not a lost platform."""
    await setup(hass, cloud)
    switch = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_test-dsn_eco_mode_switch"
    )
    assert switch
    assert hass.states.get(switch).state == STATE_UNAVAILABLE
    cloud[1]["set_eco"] = 1
    cloud[0].runtime_data.invalidate_properties()
    await async_update_entity(hass, switch)
    assert hass.states.get(switch).state == "on"


@pytest.mark.parametrize(
    "duplicate", ["device_mode", "activate_realfeel", "firmware_version"]
)
async def test_duplicate_values_are_unknown(hass, cloud, duplicate):
    """Conflicting values cannot be treated as identity or mode evidence."""
    await setup(hass, cloud)
    records = cloud[2]()
    records.append({"property": {"name": duplicate, "value": cloud[1][duplicate]}})
    cloud[3].return_value = records
    cloud[3].side_effect = None
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_not_awaited()


@pytest.mark.parametrize(
    ("source", "command", "property_name", "value", "options"),
    [
        (
            1,
            REAL_FEEL_ACTIVATION_PAYLOAD,
            "activate_realfeel",
            REAL_FEEL_DEACTIVATION_PAYLOAD,
            {"allow_redirects": False},
        ),
        (1, REAL_FEEL_DEACTIVATION_PAYLOAD, "device_mode", 1, {}),
        (2, REAL_FEEL_ACTIVATION_PAYLOAD, "device_mode", 1, {}),
        (3, REAL_FEEL_ACTIVATION_PAYLOAD, "device_mode", 1, {}),
    ],
)
async def test_dehumidify_routes_one_fresh_guarded_write(
    hass, cloud, source, command, property_name, value, options
):
    await setup(hass, cloud)
    cloud[1].update(device_mode=source, activate_realfeel=command)
    reads = cloud[3].await_count
    await set_mode(hass, "DEHUMIDIFY")
    assert cloud[3].await_count >= reads + 1
    cloud[4].assert_awaited_once_with(
        f"apiv1/dsns/test-dsn/properties/{property_name}/datapoints.json",
        {"datapoint": {"value": value}},
        **options,
    )


@pytest.mark.parametrize(
    ("source", "target", "property_name", "value"),
    [
        (1, "REAL_FEEL", "activate_realfeel", REAL_FEEL_ACTIVATION_PAYLOAD),
        (2, "REAL_FEEL", "activate_realfeel", REAL_FEEL_ACTIVATION_PAYLOAD),
        (3, "REAL_FEEL", "activate_realfeel", REAL_FEEL_ACTIVATION_PAYLOAD),
        (1, "DRY_CLOTHES", "device_mode", 2),
        (1, "PURIFIER", "device_mode", 3),
    ],
)
async def test_other_modes_keep_one_legacy_write(
    hass, cloud, source, target, property_name, value
):
    await setup(hass, cloud)
    cloud[1]["device_mode"] = source
    await set_mode(hass, target)
    cloud[4].assert_awaited_once_with(
        f"apiv1/dsns/test-dsn/properties/{property_name}/datapoints.json",
        {"datapoint": {"value": value}},
        **({"allow_redirects": False} if target == "REAL_FEEL" else {}),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_mode", True),
        ("device_mode", "1"),
        ("device_mode", 100),
        ("activate_realfeel", "unknown"),
        ("activate_realfeel", None),
        ("firmware_version", "1.1"),
        ("appliance_model", "other"),
    ],
)
async def test_fresh_dehumidify_guard_rejects_unsafe_data(hass, cloud, field, value):
    """A stale previously valid snapshot cannot authorize an incorrect command."""
    await setup(hass, cloud)
    cloud[1][field] = value
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_not_awaited()


async def test_pending_suppresses_old_echo_and_blocks_wrong_followup(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    await set_mode(hass, "REAL_FEEL")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    with pytest.raises(ServiceValidationError, match="Wait"):
        await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_awaited_once()

    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
    await set_mode(hass, "DEHUMIDIFY")
    assert cloud[4].await_count == 2
    assert cloud[4].await_args.kwargs == {"allow_redirects": False}


@pytest.mark.parametrize(
    ("cloud", "starting_mode"),
    [
        (
            {"device_mode": 2, "activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "DRY_CLOTHES",
        ),
        (
            {"device_mode": 3, "activate_realfeel": REAL_FEEL_DEACTIVATION_PAYLOAD},
            "PURIFIER",
        ),
    ],
    indirect=["cloud"],
)
async def test_late_real_feel_cloud_confirmation_refreshes_without_poll(
    hass, cloud, starting_mode
):
    """A delayed cloud confirmation updates the selector without another write."""
    await setup(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == starting_mode
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None

        # The initial post-command GET still showed the old mode.  Simulate the
        # cloud catching up after the client's ten-second cache lifetime.
        reads = cloud[3].await_count
        cloud[1].update(device_mode=1, activate_realfeel=REAL_FEEL_ACTIVATION_PAYLOAD)
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
        assert cloud[3].await_count > reads
    cloud[4].assert_awaited_once_with(
        "apiv1/dsns/test-dsn/properties/activate_realfeel/datapoints.json",
        {"datapoint": {"value": REAL_FEEL_ACTIVATION_PAYLOAD}},
        allow_redirects=False,
    )


async def test_pending_real_feel_refresh_stops_when_confirmed(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    with (
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 3600),
        patch.object(action_refresh, "_REFRESH_DELAY", 3600),
    ):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        pending = entity._pending_refresh_timer
        generation = entity._pending_generation
        assert pending is not None
        cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        await refresh(hass, cloud)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
        assert pending.cancelled()
        reads = cloud[3].await_count
        cloud[0].runtime_data.device_properties_timestamp -= 11
        entity._pending_refresh_tick(generation)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entity._pending_refresh_timer is None
        assert cloud[3].await_count == reads
    cloud[4].assert_awaited_once()


@pytest.mark.parametrize("cloud", [{"device_mode": 2}], indirect=True)
async def test_pending_refresh_stops_when_distinct_external_mode_arrives(hass, cloud):
    await setup(hass, cloud)
    with (
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 3600),
        patch.object(action_refresh, "_REFRESH_DELAY", 3600),
    ):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        pending = entity._pending_refresh_timer
        generation = entity._pending_generation
        assert pending is not None
        cloud[1]["device_mode"] = 3
        await refresh(hass, cloud)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "PURIFIER"
        assert pending.cancelled()
        reads = cloud[3].await_count
        cloud[0].runtime_data.device_properties_timestamp -= 11
        entity._pending_refresh_tick(generation)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entity._pending_refresh_timer is None
        assert cloud[3].await_count == reads
    cloud[4].assert_awaited_once()


async def test_pending_refresh_is_bounded_and_reuses_cache(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    client = cloud[0].runtime_data
    with (
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 3600),
        patch.object(action_refresh, "_REFRESH_DELAY", 3600),
        patch.object(
            client, "get_properties", wraps=client.get_properties
        ) as properties,
    ):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        reads = cloud[3].await_count
        observations = properties.await_count
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        for _ in range(4):
            timer = entity._pending_refresh_timer
            assert timer is not None
            timer.cancel()
            entity._pending_refresh_tick(entity._pending_generation)
            await hass.async_block_till_done(wait_background_tasks=True)
        assert properties.await_count - observations == 4
        assert cloud[3].await_count == reads  # The ten-second cache remains valid.
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
        assert entity._pending_refresh_timer is None
    cloud[4].assert_awaited_once()


async def test_pending_refresh_stops_after_authentication_rejection(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    client = cloud[0].runtime_data
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        reads = cloud[3].await_count
        with (
            patch.object(
                client,
                "get_new_access_token",
                side_effect=InvalidCredentialsError("rejected"),
            ),
            pytest.raises(InvalidCredentialsError),
        ):
            await client._fetch_access_token()
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert client.invalid_credentials
        assert cloud[3].await_count == reads
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        assert entity._pending_refresh_timer is None
    cloud[4].assert_awaited_once()


async def test_pending_refresh_timer_is_cancelled_on_unload(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        timer = entity._pending_refresh_timer
        reads = cloud[3].await_count
        assert timer is not None
        assert await hass.config_entries.async_unload(cloud[0].entry_id)
        assert timer.cancelled()
        await asyncio.sleep(0.15)
        assert cloud[3].await_count == reads
    cloud[4].assert_awaited_once()


async def test_pending_refresh_timer_is_cancelled_on_expiry(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        timer = entity._pending_refresh_timer
        assert timer is not None
        entity._pending_deadline = hass.loop.time() - 1
        entity._pending_timer.cancel()
        entity._pending_expired(entity._pending_generation)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert timer.cancelled()
        reads = cloud[3].await_count
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cloud[3].await_count == reads
    cloud[4].assert_awaited_once()


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_ambiguous_write_still_refreshes_without_retry(hass, cloud, failure):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    started = asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        if failure == "timeout":
            raise TimeoutError("lost response")
        await asyncio.Event().wait()

    cloud[4].side_effect = send
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        task = asyncio.create_task(set_mode(hass, "REAL_FEEL"))
        await asyncio.wait_for(started.wait(), 3)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(
            HomeAssistantError if failure == "timeout" else asyncio.CancelledError
        ):
            await task
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
        reads = cloud[3].await_count
        cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cloud[3].await_count > reads
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
    cloud[4].assert_awaited_once()


async def test_old_refresh_callback_cannot_cancel_new_request_timer(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
        old_generation = entity._pending_generation
        old_timer = entity._pending_refresh_timer
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        new_timer = entity._pending_refresh_timer
        assert old_timer is not None and old_timer.cancelled()
        assert new_timer is not None and new_timer is not old_timer
        entity._pending_refresh_tick(old_generation)
        assert entity._pending_refresh_timer is new_timer
        cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
    assert cloud[4].await_count == 2  # One for each explicit user action.


@pytest.mark.parametrize(
    ("cloud", "target"),
    [({}, "DRY_CLOTHES"), ({"firmware_version": "other"}, "REAL_FEEL")],
    indirect=["cloud"],
)
async def test_other_mode_or_device_does_not_schedule_extra_refreshes(
    hass, cloud, target
):
    await setup(hass, cloud)
    client = cloud[0].runtime_data
    with (
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05),
        patch.object(action_refresh, "_REFRESH_DELAY", 3600),
        patch.object(
            client, "get_properties", wraps=client.get_properties
        ) as properties,
    ):
        await set_mode(hass, target)
        await hass.async_block_till_done(wait_background_tasks=True)
        observations = properties.await_count
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert properties.await_count == observations
    cloud[4].assert_awaited_once()


async def test_pending_refresh_does_not_overlap_a_slow_get(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_read(_path):
        started.set()
        await release.wait()
        return cloud[2]()

    with patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.03):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        cloud[3].side_effect = slow_read
        reads = cloud[3].await_count
        cloud[0].runtime_data.device_properties_timestamp -= 11
        try:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(0.2)
            assert cloud[3].await_count == reads + 1
            cloud[4].assert_awaited_once()
            cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        finally:
            release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
    cloud[4].assert_awaited_once()


async def test_superseded_inflight_refresh_cannot_restore_old_mode(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    started, release = asyncio.Event(), asyncio.Event()

    with (
        patch.object(humidifier, "_PENDING_REFRESH_INTERVAL", 0.05),
        patch.object(action_refresh, "_REFRESH_DELAY", 3600),
    ):
        await set_mode(hass, "REAL_FEEL")
        await hass.async_block_till_done(wait_background_tasks=True)
        old_snapshot = cloud[2]()
        reads = 0

        async def read(_path):
            nonlocal reads
            reads += 1
            if reads == 1:
                started.set()
                await release.wait()
                return old_snapshot
            return cloud[2]()

        cloud[3].side_effect = read
        cloud[0].runtime_data.device_properties_timestamp -= 11
        try:
            await asyncio.wait_for(started.wait(), 3)
            await set_mode(hass, "DRY_CLOTHES")
            cloud[1]["device_mode"] = 2
        finally:
            release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "DRY_CLOTHES"
        read_count = cloud[3].await_count
        cloud[0].runtime_data.device_properties_timestamp -= 11
        await asyncio.sleep(0.15)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert cloud[3].await_count == read_count
    assert cloud[4].await_count == 2


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_ambiguous_activation_keeps_unknown_until_observation(
    hass, cloud, failure
):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    started = asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        if failure == "timeout":
            raise TimeoutError("lost response")
        await asyncio.Event().wait()

    cloud[4].side_effect = send
    task = asyncio.create_task(set_mode(hass, "REAL_FEEL"))
    await asyncio.wait_for(started.wait(), 3)
    if failure == "cancel":
        task.cancel()
    with pytest.raises(
        HomeAssistantError if failure == "timeout" else asyncio.CancelledError
    ):
        await task
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_awaited_once()
    cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"


async def test_pending_expiry_fetches_new_data_and_external_mode_recovers(hass, cloud):
    await setup(hass, cloud)
    await set_mode(hass, "DRY_CLOTHES")
    await hass.async_block_till_done(wait_background_tasks=True)
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    cloud[1]["device_mode"] = 3
    reads = cloud[3].await_count
    entity._pending_deadline = hass.loop.time() - 1
    entity._pending_timer.cancel()
    entity._pending_expired(entity._pending_generation)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert cloud[3].await_count == reads + 1
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "PURIFIER"


async def test_new_request_replaces_pending_and_old_timeout_cannot_clear_it(
    hass, cloud
):
    await setup(hass, cloud)
    await set_mode(hass, "DRY_CLOTHES")
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    old_generation = entity._pending_generation
    await set_mode(hass, "PURIFIER")
    entity._pending_expired(old_generation)
    cloud[1]["device_mode"] = 2
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    cloud[1]["device_mode"] = 3
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "PURIFIER"
    assert cloud[4].await_count == 2


async def test_guard_failure_and_lock_wait_cancellation_do_not_set_pending(hass, cloud):
    await setup(hass, cloud)
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    cloud[1]["activate_realfeel"] = "unknown"
    with pytest.raises(ServiceValidationError):
        await set_mode(hass, "DEHUMIDIFY")
    assert entity._pending_mode is None
    lock = cloud[0].runtime_data.humidifier_commands._lock
    await lock.acquire()
    task = asyncio.create_task(set_mode(hass, "REAL_FEEL"))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        lock.release()
    assert entity._pending_mode is None
    cloud[4].assert_not_awaited()


async def test_unload_cancels_pending_timer(hass, cloud):
    await setup(hass, cloud)
    await set_mode(hass, "DRY_CLOTHES")
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    timer = entity._pending_timer
    assert timer is not None and not timer.cancelled()
    assert await hass.config_entries.async_unload(cloud[0].entry_id)
    assert timer.cancelled()
    with patch.object(entity, "async_schedule_update_ha_state") as schedule:
        entity._pending_expired(entity._pending_generation)
        schedule.assert_not_called()


async def test_read_error_does_not_send_or_expose_response(hass, cloud):
    await setup(hass, cloud)
    cloud[3].side_effect = TimeoutError("secret server response")
    with pytest.raises(HomeAssistantError) as err:
        await set_mode(hass, "DEHUMIDIFY")
    assert "secret server response" not in str(err.value)
    cloud[4].assert_not_awaited()


async def test_preexisting_read_cannot_choose_stale_dehumidify_route(hass, cloud):
    """Drain an older GET before deciding which Dehumidify command to send."""
    await setup(hass, cloud)
    entry, values, snapshot, get, post = cloud
    client = entry.runtime_data
    old = snapshot()
    values["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
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
    client.invalidate_properties()
    poll = asyncio.create_task(client.get_properties())
    action = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        action = asyncio.create_task(set_mode(hass, "DEHUMIDIFY"))
        await asyncio.sleep(0.02)
        assert not action.done()
        post.assert_not_awaited()
        release.set()
        await poll
        await action
        post.assert_awaited_once_with(
            "apiv1/dsns/test-dsn/properties/device_mode/datapoints.json",
            {"datapoint": {"value": 1}},
        )
        assert calls >= 2
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (poll, action) if task is not None),
            return_exceptions=True,
        )


async def test_reauth_during_exit_preflight_cannot_write(hass, cloud):
    """A rejected token refresh aborts a pending Dehumidify request."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    client = entry.runtime_data
    started, release = asyncio.Event(), asyncio.Event()

    async def read(_path):
        started.set()
        await release.wait()
        await client.get_access_token()
        raise AssertionError("authentication should fail")

    get.side_effect = read
    with patch.object(
        client,
        "get_new_access_token",
        AsyncMock(side_effect=InvalidCredentialsError("credentials rejected")),
    ) as authenticate:
        task = asyncio.create_task(set_mode(hass, "DEHUMIDIFY"))
        try:
            await asyncio.wait_for(started.wait(), 3)
            post.assert_not_awaited()
            release.set()
            with pytest.raises(InvalidCredentialsError):
                await task
            await hass.async_block_till_done()
            authenticate.assert_awaited_once()
            assert client.invalid_credentials
            assert client.humidifier_commands.generation == 0
            assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
            post.assert_not_awaited()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_unload_during_exit_preflight_cannot_write(hass, cloud):
    """Unloading during the fresh GET cancels the Dehumidify request."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    started = asyncio.Event()

    async def read(_path):
        started.set()
        await asyncio.Event().wait()

    get.side_effect = read
    task = asyncio.create_task(set_mode(hass, "DEHUMIDIFY"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(asyncio.CancelledError):
            await task
        post.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_identity_loss_and_recovery_do_not_bypass_dehumidify_guard(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["firmware_version"] = "1.1"
    await refresh(hass, cloud)
    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["mode"] is None
    assert "REAL_FEEL" not in state.attributes["available_modes"]
    with pytest.raises(ServiceValidationError, match="firmware"):
        await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_not_awaited()
    cloud[1]["firmware_version"] = "1.0"
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"


@pytest.mark.parametrize("cloud", [{"firmware_version": "other"}], indirect=True)
async def test_other_device_keeps_original_direct_mode_path(hass, cloud):
    await setup(hass, cloud)
    reads = cloud[3].await_count

    async def write(_path, _data):
        assert cloud[3].await_count == reads
        return {}

    cloud[4].side_effect = write
    await set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_awaited_once_with(
        "apiv1/dsns/test-dsn/properties/device_mode/datapoints.json",
        {"datapoint": {"value": 1}},
    )


async def test_old_poll_cannot_overwrite_pending_or_post_command_observation(
    hass, cloud
):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    old_snapshot = cloud[2]()
    started, release, fresh_started, release_fresh = (asyncio.Event() for _ in range(4))
    reads = 0

    async def read(_path):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            await release.wait()
            return old_snapshot
        fresh_started.set()
        await release_fresh.wait()
        return cloud[2]()

    cloud[3].side_effect = read
    cloud[0].runtime_data.invalidate_properties()
    old = asyncio.create_task(async_update_entity(hass, humidifier_id(hass)))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await set_mode(hass, "REAL_FEEL")
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
        cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        release.set()
        await old
        await asyncio.wait_for(fresh_started.wait(), 3)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
        release_fresh.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
        cloud[4].assert_awaited_once()
    finally:
        release.set()
        release_fresh.set()
        await asyncio.gather(old, return_exceptions=True)


async def test_observation_during_write_does_not_clear_pending(hass, cloud):
    await setup(hass, cloud)
    started, release = asyncio.Event(), asyncio.Event()

    async def write(*args, **kwargs):
        cloud[1]["device_mode"] = 2
        started.set()
        await release.wait()
        return {}

    cloud[4].side_effect = write
    task = asyncio.create_task(set_mode(hass, "DRY_CLOTHES"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await refresh(hass, cloud)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
        release.set()
        await task
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "DRY_CLOTHES"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_exit_write_failure_keeps_pending_and_hides_transport_details(
    hass, cloud
):
    await setup(hass, cloud)
    cloud[4].side_effect = TimeoutError("secret response")
    with pytest.raises(HomeAssistantError) as err:
        await set_mode(hass, "DEHUMIDIFY")
    assert "secret response" not in str(err.value)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    cloud[4].assert_awaited_once()


async def test_external_third_mode_clears_pending_during_guard(hass, cloud):
    """A distinct ordinary cloud mode can supersede an unconfirmed request."""
    await setup(hass, cloud)
    await set_mode(hass, "DRY_CLOTHES")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] is None
    cloud[1]["device_mode"] = 3
    await set_mode(hass, "DEHUMIDIFY")
    assert cloud[4].await_count == 2
    cloud[4].assert_awaited_with(
        "apiv1/dsns/test-dsn/properties/device_mode/datapoints.json",
        {"datapoint": {"value": 1}},
    )


async def test_external_third_mode_clears_pending_during_poll(hass, cloud):
    await setup(hass, cloud)
    await set_mode(hass, "DRY_CLOTHES")
    cloud[1]["device_mode"] = 3
    await refresh(hass, cloud)
    assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "PURIFIER"


@pytest.mark.parametrize("cloud", [{"firmware_version": "other"}], indirect=True)
async def test_device_discovered_while_command_waits_uses_guarded_path(hass, cloud):
    await setup(hass, cloud)
    lock = cloud[0].runtime_data.humidifier_commands._lock
    await lock.acquire()
    task = asyncio.create_task(set_mode(hass, "DEHUMIDIFY"))
    try:
        await asyncio.sleep(0)
        cloud[1]["firmware_version"] = "1.0"
        await refresh(hass, cloud)
        cloud[4].assert_not_awaited()
    finally:
        lock.release()
    await task
    cloud[4].assert_awaited_once_with(
        "apiv1/dsns/test-dsn/properties/activate_realfeel/datapoints.json",
        {"datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}},
        allow_redirects=False,
    )


async def test_preflight_spanning_pending_expiry_cannot_route_stale_read(hass, cloud):
    await setup(hass, cloud)
    cloud[1]["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    await refresh(hass, cloud)
    await set_mode(hass, "REAL_FEEL")
    await hass.async_block_till_done(wait_background_tasks=True)
    entity = hass.data["humidifier"].get_entity(humidifier_id(hass))
    entity._pending_deadline = hass.loop.time() + 60
    old = cloud[2]()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def read(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old
        return cloud[2]()

    cloud[3].side_effect = read
    task = asyncio.create_task(set_mode(hass, "DEHUMIDIFY"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        cloud[1]["activate_realfeel"] = REAL_FEEL_ACTIVATION_PAYLOAD
        entity._pending_timer.cancel()
        entity._pending_expired(entity._pending_generation)
        with patch.object(hass.loop, "time", return_value=entity._pending_deadline + 1):
            release.set()
            with pytest.raises(ServiceValidationError, match="Wait"):
                await task
        await hass.async_block_till_done(wait_background_tasks=True)
        cloud[4].assert_awaited_once()
        assert hass.states.get(humidifier_id(hass)).attributes["mode"] == "REAL_FEEL"
        await set_mode(hass, "DEHUMIDIFY")
        assert cloud[4].await_count == 2
        assert cloud[4].await_args.kwargs == {"allow_redirects": False}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("status", [307, 308])
async def test_supported_activation_redirect_is_not_followed(status):
    client, session, response = make_client(status, {"error": "secret response"})
    with pytest.raises(HomeAssistantError):
        await client.request_enter_real_feel()
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False
    assert session.calls[0][2]["json"] == {
        "datapoint": {"value": REAL_FEEL_ACTIVATION_PAYLOAD}
    }
    assert response.json_calls == 0
