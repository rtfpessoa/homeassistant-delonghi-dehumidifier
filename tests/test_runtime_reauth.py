"""Runtime authentication failures through Home Assistant entity updates."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests.identity_helpers import bound_entry as MockConfigEntry
from tests.test_reauth import ProviderSession, get_reauth_form

VALUES = {
    "appliance_model": "DDSX220",
    "alarm_state": 3,
    "firmware_version": "1",
    "hardware_version": "1",
    "current_humidity": 50,
    "humidity_setpoint": 55,
    "current_speed": 1,
    "device_mode": 1,
    "device_status": 1,
    "filter_change_alarm": 0,
    "filter_life": 90,
    "filter_status": 1,
    "heat_exchanger_temp": 220,
    "room_temp": 210,
    "swing": 0,
    "set_eco": 0,
}
PROPERTIES = [{"product_name": "Test"}] + [
    {"name": name, "value": value} for name, value in VALUES.items()
]


class RevocableSession(ProviderSession):
    """Accept login during setup, then reject the previously valid password."""

    def __init__(self):
        super().__init__()
        self.revoked = False
        self.error_code = 403042
        self.pause = False

    async def post(self, url, **kwargs):
        if (
            url.endswith("/accounts.login")
            and self.revoked
            and kwargs["data"]["password"] == "formerly-valid-password"
        ):
            data = kwargs["data"]
            self.logins.append((data["loginID"], data["lang"], data["password"]))
            if self.pause:
                self.login_started.set()
                await self.continue_login.wait()
            await asyncio.sleep(0.01)
            return self.json_response({"errorCode": self.error_code})
        return await super().post(url, **kwargs)


@pytest.fixture
async def loaded_entry(hass):
    session = RevocableSession()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_LANGUAGE: "en",
            CONF_PASSWORD: "formerly-valid-password",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=session,
        ),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data.device_dsn = "test-dsn"
    entry.runtime_data.token_expiry = 0
    entry.runtime_data.refresh_token = None
    session.logins.clear()
    session.revoked = True
    registry = er.async_get(hass)
    entity_ids = [
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "sensor" and hass.states.get(item.entity_id) is not None
    ]
    yield entry, session, entity_ids
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_runtime_invalid_credentials_start_reauth(hass, loaded_entry):
    entry, session, entity_ids = loaded_entry
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert len(session.logins) == 1


async def test_invalid_credentials_during_platform_setup_start_reauth(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_LANGUAGE: "en",
            CONF_PASSWORD: "password",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(
            APIClient,
            "get_new_access_token",
            AsyncMock(side_effect=InvalidCredentialsError("rejected")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def test_setup_auth_failure_unloads_previously_added_entities(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_LANGUAGE: "en",
            CONF_PASSWORD: "password",
        },
    )
    entry.add_to_hass(hass)
    forward = hass.config_entries.async_forward_entry_setups
    entity_ids = []

    async def forward_then_reject(entry, platforms):
        await forward(entry, platforms)
        entity_ids.extend(
            item.entity_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        )
        with pytest.raises(InvalidCredentialsError):
            await entry.runtime_data.get_access_token()

    with (
        patch.object(
            hass.config_entries, "async_forward_entry_setups", forward_then_reject
        ),
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)),
        patch.object(
            APIClient,
            "get_new_access_token",
            AsyncMock(side_effect=InvalidCredentialsError("rejected")),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entity_ids
    remaining = {entity_id: hass.states.get(entity_id) for entity_id in entity_ids}
    assert all(
        state is None or state.state == "unavailable" for state in remaining.values()
    ), remaining
    assert not entry.runtime_data._auth_listeners


async def test_concurrent_invalid_credentials_do_not_cause_login_storm(
    hass, loaded_entry
):
    _, session, entity_ids = loaded_entry
    assert len(entity_ids) >= 10
    await asyncio.gather(
        *(async_update_entity(hass, entity_id) for entity_id in entity_ids)
    )
    await hass.async_block_till_done()
    assert len(session.logins) == 1


async def test_invalid_auth_marks_polled_entities_unavailable(hass, loaded_entry):
    _entry, _, sensor_ids = loaded_entry
    registry = er.async_get(hass)
    humidifier_id = registry.async_get_entity_id(
        "humidifier", DOMAIN, f"{DOMAIN}_test-dsn_dehumidifier"
    )
    ids = [*sensor_ids, humidifier_id]
    await asyncio.gather(*(async_update_entity(hass, entity_id) for entity_id in ids))
    assert all(hass.states.get(entity_id).state == "unavailable" for entity_id in ids)


async def test_alarm_and_sensors_share_one_failed_login_and_reauth(hass, loaded_entry):
    _entry, session, sensor_ids = loaded_entry
    alarm_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_test-dsn_device_alarm_binary_sensor"
    )
    assert hass.states.get(alarm_id).state == "on"
    await asyncio.gather(
        *(async_update_entity(hass, entity_id) for entity_id in [alarm_id, *sensor_ids])
    )
    await hass.async_block_till_done()
    assert hass.states.get(alarm_id).state == "unavailable"
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_rejected_credentials_are_not_retried_during_polling(hass, loaded_entry):
    _, session, entity_ids = loaded_entry
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    await asyncio.gather(
        *(async_update_entity(hass, entity_id) for entity_id in entity_ids)
    )
    await hass.async_block_till_done()
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_cancelled_reauth_does_not_reopen_during_polling(hass, loaded_entry):
    _, session, entity_ids = loaded_entry
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    hass.config_entries.flow.async_abort(flow["flow_id"])
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(session.logins) == 1


async def test_runtime_reauth_reloads_entry_and_recovers_entities(hass, loaded_entry):
    entry, session, entity_ids = loaded_entry
    old_client = entry.runtime_data
    await async_update_entity(hass, entity_ids[0])
    form = await get_reauth_form(hass, entry)
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=session,
        ),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.runtime_data is not old_client
    assert entry.runtime_data.password == "new-password"
    assert hass.states.get(entity_ids[0]).state == "50"
    alarm_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_test-dsn_device_alarm_binary_sensor"
    )
    assert hass.states.get(alarm_id).state == "on"


@pytest.mark.parametrize("error_code", [403120, 500001])
async def test_other_provider_failures_remain_retryable(hass, loaded_entry, error_code):
    _, session, entity_ids = loaded_entry
    session.error_code = error_code
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    assert len(session.logins) == 2
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_network_failure_does_not_start_reauth_and_can_retry(hass, loaded_entry):
    _, session, entity_ids = loaded_entry
    session.connection_error = True
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    session.connection_error = False
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_rejected_login_does_not_log_credentials(hass, loaded_entry, caplog):
    _, _, entity_ids = loaded_entry
    caplog.set_level(logging.DEBUG)
    await async_update_entity(hass, entity_ids[0])
    await hass.async_block_till_done()
    assert "formerly-valid-password" not in caplog.text
    assert "test@example.com" not in caplog.text


async def test_cancelling_a_waiter_does_not_cancel_shared_login(hass, loaded_entry):
    entry, session, _ = loaded_entry
    session.pause = True
    leader = asyncio.create_task(entry.runtime_data.get_access_token())
    await asyncio.wait_for(session.login_started.wait(), timeout=5)
    waiter = asyncio.create_task(entry.runtime_data.get_access_token())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    session.continue_login.set()
    with pytest.raises(InvalidCredentialsError):
        await leader
    await hass.async_block_till_done()
    assert len(session.logins) == 1
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_cancelling_login_owner_preserves_invalid_auth_for_waiters(
    hass, loaded_entry
):
    entry, session, _ = loaded_entry
    session.pause = True
    leader = asyncio.create_task(entry.runtime_data.get_access_token())
    await asyncio.wait_for(session.login_started.wait(), timeout=5)
    waiter = asyncio.create_task(entry.runtime_data.get_access_token())
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    session.continue_login.set()
    with pytest.raises(InvalidCredentialsError):
        await waiter
    assert len(session.logins) == 1


async def test_inflight_failure_after_unload_does_not_start_reauth(hass, loaded_entry):
    entry, session, _ = loaded_entry
    client = entry.runtime_data
    session.pause = True
    task = asyncio.create_task(client.get_access_token())
    await asyncio.wait_for(session.login_started.wait(), timeout=5)
    assert await hass.config_entries.async_unload(entry.entry_id)
    session.continue_login.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert client._token_request is None
    assert not client._auth_listeners


async def test_old_client_failure_after_reauth_cannot_start_another_flow(
    hass, loaded_entry
):
    entry, session, _ = loaded_entry
    session.pause = True
    old_client = entry.runtime_data
    task = asyncio.create_task(old_client.get_access_token())
    await asyncio.wait_for(session.login_started.wait(), timeout=5)
    entry.async_start_reauth(hass)
    form = await get_reauth_form(hass, entry)
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=session,
        ),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.runtime_data is not old_client
    session.continue_login.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_concurrent_transient_failure_is_shared_and_later_retryable(hass):
    client = APIClient(None, "en", "test@example.com", "password")
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def failing_login():
        started.set()
        await proceed.wait()
        raise aiohttp.ClientConnectionError("temporary failure")

    client.get_new_access_token = AsyncMock(side_effect=failing_login)
    tasks = [asyncio.create_task(client.get_access_token()) for _ in range(12)]
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0)
    proceed.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, aiohttp.ClientConnectionError) for result in results)
    assert client.get_new_access_token.await_count == 1
    with pytest.raises(aiohttp.ClientConnectionError):
        await client.get_access_token()
    assert client.get_new_access_token.await_count == 2


async def test_concurrent_successful_token_requests_share_one_login():
    client = APIClient(None, "en", "test@example.com", "password")
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def successful_login():
        started.set()
        await proceed.wait()
        client.access_token = "new-token"
        client.token_expiry = time.time() + 3600
        return client.access_token

    client.get_new_access_token = AsyncMock(side_effect=successful_login)
    tasks = [asyncio.create_task(client.get_access_token()) for _ in range(12)]
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0)
    proceed.set()
    assert await asyncio.gather(*tasks) == ["new-token"] * 12
    assert await client.get_access_token() == "new-token"
    assert client.get_new_access_token.await_count == 1


async def test_runtime_switch_command_starts_reauth(hass, loaded_entry):
    _entry, session, _ = loaded_entry
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_test-dsn_eco_mode_switch"
    )
    with pytest.raises(InvalidCredentialsError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    assert len(session.logins) == 1


async def test_cancelling_leader_does_not_cancel_live_waiter():
    client = APIClient(None, "en", "test@example.com", "password")
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def successful_login():
        started.set()
        await proceed.wait()
        return "new-token"

    client.get_new_access_token = AsyncMock(side_effect=successful_login)
    leader = asyncio.create_task(client.get_access_token())
    await asyncio.wait_for(started.wait(), timeout=5)
    waiter = asyncio.create_task(client.get_access_token())
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    proceed.set()
    try:
        result = await waiter
    except asyncio.CancelledError:
        pytest.fail("cancelling one caller cancelled the shared login")
    assert result == "new-token"
    assert client.get_new_access_token.await_count == 1


async def test_rejection_marks_all_entities_unavailable_with_fresh_cache(
    hass, loaded_entry
):
    entry, _, _ = loaded_entry
    client = entry.runtime_data
    client.device_properties = PROPERTIES
    client.device_properties_timestamp = time.time()
    registry = er.async_get(hass)
    entities = [
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if hass.states.get(item.entity_id) is not None
    ]
    switch_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_test-dsn_eco_mode_switch"
    )
    with pytest.raises(InvalidCredentialsError):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": switch_id}, blocking=True
        )
    await hass.async_block_till_done()
    assert {entity_id.split(".")[0] for entity_id in entities} == {
        "sensor",
        "switch",
        "humidifier",
        "binary_sensor",
    }
    assert all(
        hass.states.get(entity_id).state == "unavailable" for entity_id in entities
    )


async def test_cached_alarm_cannot_recover_after_auth_rejection(hass, loaded_entry):
    entry, _, _ = loaded_entry
    client = entry.runtime_data
    client.device_properties = PROPERTIES
    client.device_properties_timestamp = time.time()
    alarm_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_test-dsn_device_alarm_binary_sensor"
    )
    with pytest.raises(InvalidCredentialsError):
        await client.get_access_token()
    await async_update_entity(hass, alarm_id)
    assert hass.states.get(alarm_id).state == "unavailable"


async def test_queued_reauth_does_not_start_after_unload(hass, loaded_entry):
    entry, _, entity_ids = loaded_entry
    await entry._reauth_lock.acquire()
    try:
        await async_update_entity(hass, entity_ids[0])
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        entry._reauth_lock.release()
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_queued_reauth_does_not_start_after_removing_entry(hass, loaded_entry):
    entry, _, entity_ids = loaded_entry
    await entry._reauth_lock.acquire()
    try:
        await async_update_entity(hass, entity_ids[0])
        assert await hass.config_entries.async_remove(entry.entry_id)
    finally:
        entry._reauth_lock.release()
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_queued_old_client_reauth_does_not_start_after_reload(hass, loaded_entry):
    entry, session, entity_ids = loaded_entry
    old_client = entry.runtime_data
    await entry._reauth_lock.acquire()
    try:
        await async_update_entity(hass, entity_ids[0])
        session.revoked = False
        with (
            patch(
                "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
                return_value=session,
            ),
            patch.object(
                APIClient, "get_first_device", AsyncMock(return_value="test-dsn")
            ),
            patch.object(
                APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])
            ),
            patch.object(
                APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)
            ),
        ):
            assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not old_client
    finally:
        entry._reauth_lock.release()
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_abandoned_login_is_cancelled_and_its_exception_consumed():
    client = APIClient(None, "en", "test@example.com", "password")
    started = asyncio.Event()
    finished = asyncio.Event()

    async def cancelled_login():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            finished.set()
            raise aiohttp.ClientConnectionError("failure after cancellation")

    client.get_new_access_token = AsyncMock(side_effect=cancelled_login)
    loop = asyncio.get_running_loop()
    with patch.object(loop, "call_exception_handler") as handler:
        caller = asyncio.create_task(client.get_access_token())
        await asyncio.wait_for(started.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.wait_for(finished.wait(), timeout=5)
        await asyncio.sleep(0)
        assert client._token_request is None
        handler.assert_not_called()
