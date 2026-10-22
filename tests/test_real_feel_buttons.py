"""Exercise the guarded DDSX220 requests through Home Assistant."""

import asyncio
import traceback
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
)
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests.test_client_http import make_client


@pytest.fixture
async def cloud(hass, request):
    """Load a mutable simulated device, without contacting the cloud."""
    values = {
        "product_name": "Test",
        "appliance_model": "DDSX220",
        "firmware_version": "1.0",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "activate_realfeel": REAL_FEEL_ACTIVATION_PAYLOAD,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
    }
    values.update(getattr(request, "param", {}))

    def snapshot():
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in values.items()
        ]

    async def read(_path):
        return snapshot()

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.invalid",
            CONF_PASSWORD: "test-password",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=read)) as get,
        patch.object(APIClient, "post_request", AsyncMock(return_value={})) as post,
    ):
        yield entry, values, snapshot, get, post
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def setup(hass, cloud):
    entry, *_ = cloud
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def button_id(hass):
    return er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{DOMAIN}_test-dsn_request_exit_real_feel"
    )


async def press(hass):
    return await hass.services.async_call(
        "button", "press", {"entity_id": button_id(hass)}, blocking=True
    )


@pytest.mark.parametrize(
    ("cloud", "supported"),
    [
        ({}, True),
        ({"firmware_version": 1.0}, True),
        ({"firmware_version": "1"}, False),
        ({"firmware_version": 1}, False),
        ({"firmware_version": True}, False),
        ({"firmware_version": None}, False),
        ({"firmware_version": "1.1"}, False),
        ({"appliance_model": "DDSX220WFA"}, False),
        ({"appliance_model": None}, False),
    ],
    indirect=["cloud"],
)
async def test_discovery_is_model_and_firmware_specific(hass, cloud, supported):
    """Unsupported or unknown devices do not get these controls or writes."""
    await setup(hass, cloud)
    assert bool(button_id(hass)) is supported
    cloud[4].assert_not_awaited()


@pytest.mark.parametrize(
    ("language", "name"),
    [
        ("en", "Request exit from Real Feel"),
        ("de", "Beenden von Real Feel anfordern"),
        ("fr", "Request exit from Real Feel"),
    ],
)
async def test_discoverable_localized_config_button(hass, cloud, language, name):
    """The button has a stable ID, translated name and CONFIG category."""
    hass.config.language = language
    await setup(hass, cloud)
    entity = er.async_get(hass).async_get(button_id(hass))
    translations = await async_get_translations(hass, language, "entity", [DOMAIN])
    assert entity.entity_category == EntityCategory.CONFIG
    assert entity.original_name == name
    assert (
        translations[f"component.{DOMAIN}.entity.button.request_exit_real_feel.name"]
        == name
    )
    cloud[4].assert_not_awaited()


@pytest.mark.parametrize("language", ["en", "de"])
async def test_guard_errors_are_actionable_and_translated(hass, cloud, language):
    """A rejected request exposes localized guidance without cloud values."""
    await setup(hass, cloud)
    cloud[1]["device_mode"] = 2
    with pytest.raises(ServiceValidationError, match="Select Real Feel") as err:
        await press(hass)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "real_feel_not_ready"
    translations = await async_get_translations(hass, language, "exceptions", [DOMAIN])
    text = translations[f"component.{DOMAIN}.exceptions.real_feel_not_ready.message"]
    assert "Real Feel" in text
    assert "De'Longhi" in text
    cloud[4].assert_not_awaited()


async def test_device_and_area_button_targets_do_not_press(hass, cloud):
    """HA expands device and area targets only to primary entities."""
    await setup(hass, cloud)
    button = er.async_get(hass).async_get(button_id(hass))
    area = ar.async_get(hass).async_create("Test room")
    dr.async_get(hass).async_update_device(button.device_id, area_id=area.id)
    for target in ({"device_id": button.device_id}, {"area_id": area.id}):
        await hass.services.async_call("button", "press", target, blocking=True)
    cloud[4].assert_not_awaited()


async def test_all_buttons_target_can_press_the_exit_button(hass, cloud):
    """CONFIG is excluded for area/device, but explicit all includes this button."""
    await setup(hass, cloud)
    await hass.services.async_call(
        "button", "press", {"entity_id": "all"}, blocking=True
    )
    cloud[4].assert_awaited_once()
    assert cloud[4].await_args.args[1] == {
        "datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}
    }


@pytest.mark.parametrize("failure", ["guard", "send"])
async def test_last_pressed_is_only_an_attempt(hass, cloud, failure):
    """HA records a press even if validation or the cloud write fails."""
    await setup(hass, cloud)
    if failure == "guard":
        cloud[1]["device_mode"] = 2
    else:
        cloud[4].side_effect = TimeoutError("uncertain outcome")
    before = hass.states.get(button_id(hass)).state
    with pytest.raises(HomeAssistantError):
        await press(hass)
    after = hass.states.get(button_id(hass)).state
    assert before != after
    assert "T" in after
    assert cloud[4].await_count == (failure == "send")


@pytest.mark.parametrize("cloud", [{}, {"firmware_version": 1.0}], indirect=True)
async def test_request_uses_fresh_snapshot_and_one_fixed_write(hass, cloud):
    """Only the tested exit command is sent after invalidating the cache."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    reads = get.await_count
    await press(hass)
    assert get.await_count >= reads + 1
    post.assert_awaited_once_with(
        "apiv1/dsns/test-dsn/properties/activate_realfeel/datapoints.json",
        {"datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}},
        allow_redirects=False,
    )
    assert entry.runtime_data.humidifier_commands.generation == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("appliance_model", None, "firmware"),
        ("appliance_model", "DDSX220WFA", "firmware"),
        ("firmware_version", None, "firmware"),
        ("firmware_version", True, "firmware"),
        ("firmware_version", "1.1", "firmware"),
        ("device_mode", None, "Select Real Feel"),
        ("device_mode", True, "Select Real Feel"),
        ("device_mode", 1.0, "Select Real Feel"),
        ("device_mode", "1", "Select Real Feel"),
        ("device_mode", 2, "Select Real Feel"),
        ("activate_realfeel", None, "Select Real Feel"),
        ("activate_realfeel", True, "Select Real Feel"),
        ("activate_realfeel", "unexpected secret", "Select Real Feel"),
    ],
)
async def test_rejects_unknown_or_unexpected_fresh_values(
    hass, cloud, field, value, message
):
    """A changed or malformed precondition cannot send a command."""
    await setup(hass, cloud)
    entry, values, _, _, post = cloud
    values[field] = value
    with pytest.raises(ServiceValidationError, match=message):
        await press(hass)
    post.assert_not_awaited()
    assert entry.runtime_data.humidifier_commands.generation == 0


async def test_rejects_same_command(hass, cloud):
    """The already-requested exit command is not accepted as an activation."""
    await setup(hass, cloud)
    _, values, _, _, post = cloud
    values["activate_realfeel"] = REAL_FEEL_DEACTIVATION_PAYLOAD
    with pytest.raises(ServiceValidationError, match="Select Real Feel"):
        await press(hass)
    post.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("appliance_model", "firmware"),
        ("firmware_version", "firmware"),
        ("device_mode", "Select Real Feel"),
        ("activate_realfeel", "Select Real Feel"),
    ],
)
@pytest.mark.parametrize("issue", ["missing", "duplicate"])
async def test_rejects_missing_or_duplicate_required_fields(
    hass, cloud, field, message, issue
):
    """A guard value must appear exactly once in the current snapshot."""
    await setup(hass, cloud)
    _, values, snapshot, get, post = cloud
    if issue == "missing":
        del values[field]
    else:
        duplicate = {"property": {"name": field, "value": values[field]}}
        get.side_effect = lambda _path: snapshot() + [duplicate]
    with pytest.raises(ServiceValidationError, match=message):
        await press(hass)
    post.assert_not_awaited()


async def test_old_inflight_get_cannot_satisfy_fresh_guard(hass, cloud):
    """A request begun before the press drains, then a new GET decides the guard."""
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
        action = asyncio.create_task(press(hass))
        await asyncio.sleep(0.02)
        assert not action.done()
        post.assert_not_awaited()
        release.set()
        await poll
        with pytest.raises(ServiceValidationError, match="Select Real Feel"):
            await action
        assert calls == 2
        post.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (poll, action) if task is not None),
            return_exceptions=True,
        )


async def test_guard_and_write_are_serialized_with_humidifier_commands(hass, cloud):
    """A humidifier command waits until the guarded exit request completes."""
    await setup(hass, cloud)
    entry, _, snapshot, get, post = cloud
    started, release = asyncio.Event(), asyncio.Event()
    registry = er.async_get(hass)
    humidifier = registry.async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )

    async def read(_path):
        started.set()
        await release.wait()
        return snapshot()

    get.side_effect = read
    action = asyncio.create_task(press(hass))
    other = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        other = asyncio.create_task(
            hass.services.async_call(
                "humidifier", "turn_off", {"entity_id": humidifier}, blocking=True
            )
        )
        await asyncio.sleep(0.02)
        post.assert_not_awaited()
        release.set()
        await asyncio.gather(action, other)
        assert post.await_count == 2
        assert "activate_realfeel" in post.await_args_list[0].args[0]
        assert "set_status" in post.await_args_list[1].args[0]
        assert entry.runtime_data.humidifier_commands.generation == 2
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (action, other) if task is not None),
            return_exceptions=True,
        )


async def test_poll_started_before_write_completion_cannot_win(hass, cloud):
    """The actual button uses the shared generation to suppress a stale poll."""
    await setup(hass, cloud)
    entry, values, snapshot, get, post = cloud
    client = entry.runtime_data
    humidifier = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    post_started, post_release, poll_started, poll_release = (
        asyncio.Event() for _ in range(4)
    )
    reads = 0

    async def read(_path):
        nonlocal reads
        reads += 1
        if reads == 2:
            old = snapshot()
            poll_started.set()
            await poll_release.wait()
            return old
        return snapshot()

    async def send(*args, **kwargs):
        post_started.set()
        await post_release.wait()
        values["device_status"] = 2
        return {}

    get.side_effect = read
    post.side_effect = send
    action = asyncio.create_task(press(hass))
    poll = None
    try:
        await asyncio.wait_for(post_started.wait(), 3)
        client.invalidate_properties()
        poll = asyncio.create_task(async_update_entity(hass, humidifier))
        await asyncio.wait_for(poll_started.wait(), 3)
        post_release.set()
        await action
        poll_release.set()
        await poll
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass.states.get(humidifier).state == "off"
        assert reads >= 3
        post.assert_awaited_once()
    finally:
        post_release.set()
        poll_release.set()
        await asyncio.gather(
            *(task for task in (action, poll) if task is not None),
            return_exceptions=True,
        )


async def test_cancel_while_waiting_for_other_command_does_not_read_or_write(
    hass, cloud
):
    """Canceling an exit request waiting on the lock has no side effect."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    humidifier = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await release.wait()
        return {}

    post.side_effect = send
    other = asyncio.create_task(
        hass.services.async_call(
            "humidifier", "turn_off", {"entity_id": humidifier}, blocking=True
        )
    )
    action = None
    try:
        await asyncio.wait_for(started.wait(), 3)
        reads = get.await_count
        action = asyncio.create_task(press(hass))
        await asyncio.sleep(0.02)
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        assert get.await_count == reads
        post.assert_awaited_once()
        release.set()
        await other
        assert entry.runtime_data.humidifier_commands.generation == 1
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (other, action) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_ambiguous_write_invalidates_and_refreshes(hass, cloud, outcome):
    """An attempted write is never retried, even if its outcome is unknown."""
    await setup(hass, cloud)
    entry, values, _, get, post = cloud
    client = entry.runtime_data
    started = asyncio.Event()
    reads = get.await_count

    async def send(*args, **kwargs):
        values["device_status"] = 2
        started.set()
        if outcome == "timeout":
            raise TimeoutError("secret URL")
        await asyncio.Event().wait()

    post.side_effect = send
    task = asyncio.create_task(press(hass))
    await asyncio.wait_for(started.wait(), 3)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(HomeAssistantError, match="may have received") as err:
            await task
        assert "secret URL" not in "".join(traceback.format_exception(err.value))
    await hass.async_block_till_done(wait_background_tasks=True)
    post.assert_awaited_once()
    assert client.humidifier_commands.generation == 1
    assert get.await_count >= reads + 2
    humidifier = er.async_get(hass).async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    assert hass.states.get(humidifier).state == "off"


async def test_failed_fresh_read_sends_nothing_and_hides_transport_details(hass, cloud):
    """A failed preflight is a safe action error, never a device write."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    get.side_effect = TimeoutError("secret URL")
    with pytest.raises(HomeAssistantError, match="check the current device") as err:
        await press(hass)
    assert "secret URL" not in "".join(traceback.format_exception(err.value))
    assert entry.runtime_data.humidifier_commands.generation == 0
    post.assert_not_awaited()


async def test_reauth_during_preflight_cannot_write(hass, cloud):
    """A rejected credential refresh is propagated and does not cause a retry."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    client = entry.runtime_data

    async def read(_path):
        await client.get_access_token()
        raise AssertionError("authentication should fail")

    get.side_effect = read
    with patch.object(
        client,
        "get_new_access_token",
        AsyncMock(side_effect=InvalidCredentialsError("credentials rejected")),
    ) as authenticate:
        with pytest.raises(InvalidCredentialsError):
            await press(hass)
        await hass.async_block_till_done()
        authenticate.assert_awaited_once()
    assert client.invalid_credentials
    assert client.humidifier_commands.generation == 0
    post.assert_not_awaited()


async def test_unload_during_preflight_cannot_write(hass, cloud):
    """Removing the entry during its fresh GET cannot launch a later command."""
    await setup(hass, cloud)
    entry, _, _, get, post = cloud
    started = asyncio.Event()

    async def read(_path):
        started.set()
        await asyncio.Event().wait()

    get.side_effect = read
    task = asyncio.create_task(press(hass))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(asyncio.CancelledError):
            await task
        post.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("status", [201, 307])
async def test_transport_sends_one_post_and_does_not_follow_redirects(status):
    """A redirect cannot replay a command or cause a retry."""
    client, session, _ = make_client(status, {})
    if status == 307:
        with pytest.raises(HomeAssistantError, match="307"):
            await client.request_exit_real_feel()
    else:
        await client.request_exit_real_feel()
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/properties/activate_realfeel/datapoints.json")
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"] == {"datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}}
