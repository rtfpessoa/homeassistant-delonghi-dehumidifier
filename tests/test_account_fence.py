"""Fence stale runtime clients around Home Assistant lifecycle changes."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_component
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.delonghi_dehumidifier_api import identity
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    OffOnStatus,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_runtime_reauth
from tests.identity_helpers import bound_entry
from tests.test_multidevice_commands import Cloud, devices, posts
from tests.test_runtime_reauth import PROPERTIES

loaded_entry = test_runtime_reauth.loaded_entry


@pytest.fixture
def cloud():
    return Cloud(
        {"old-token": {"devices": devices("selected"), "values": {"selected": 50}}}
    )


@pytest.fixture
def entry(hass):
    entry = bound_entry(
        domain=DOMAIN,
        dsn="selected",
        data={
            CONF_EMAIL: "old@example.test",
            CONF_PASSWORD: "old-secret",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def provider(cloud):
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=cloud,
        ),
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["selected"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=PROPERTIES)),
        patch.object(
            APIClient, "get_access_token", AsyncMock(return_value="old-token")
        ),
    ):
        yield


def swing_id(hass, entry):
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("_swing_mode_switch")
    )


def binding_issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"device_binding_{entry.entry_id}"
    )


def platform_entities(hass):
    """Capture the active entity objects, not just persisted registry rows."""
    components = hass.data[entity_component.DATA_INSTANCES]
    return {
        domain: {entity.entity_id for entity in components[domain].entities}
        for domain in ("binary_sensor", "humidifier", "sensor", "switch")
    }


@pytest.mark.parametrize(
    "field,value",
    [
        (CONF_EMAIL, "new@example.test"),
        (CONF_PASSWORD, "new-secret"),
        (CONF_LANGUAGE, "de"),
    ],
)
async def test_settings_changed_while_waiting_for_journal(
    hass, entry, cloud, provider, field, value
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    started, proceed = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def gate(func, *args):
        if func is identity._read_journal:
            started.set()
            await proceed.wait()
        return await original(func, *args)

    await entry.setup_lock.acquire()
    task = None
    try:
        with patch.object(hass, "async_add_executor_job", gate):
            task = asyncio.create_task(
                hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": swing_id(hass, entry)},
                    blocking=True,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            hass.config_entries.async_update_entry(entry, options={field: value})
            proceed.set()
            with pytest.raises(HomeAssistantError):
                await task
            assert not posts(cloud)
            assert not binding_issue(hass, entry)
    finally:
        proceed.set()
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_fences_queued_command_and_reload_resumes(
    hass, entry, cloud, provider
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    old_client = entry.runtime_data
    started, proceed = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def gate(func, *args):
        if func is identity._read_journal:
            started.set()
            await proceed.wait()
        return await original(func, *args)

    await entry.setup_lock.acquire()
    try:
        with patch.object(hass, "async_add_executor_job", gate):
            command = asyncio.create_task(old_client.set_swing(OffOnStatus.ON))
            await asyncio.wait_for(started.wait(), timeout=5)
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            flow = await hass.config_entries.options.async_configure(
                flow["flow_id"],
                {
                    CONF_EMAIL: "new@example.test",
                    CONF_PASSWORD: "new-secret",
                    CONF_LANGUAGE: "de",
                },
            )
            assert flow["type"] == "create_entry"
            proceed.set()
            with pytest.raises(HomeAssistantError):
                await command
            assert not posts(cloud)
            assert not binding_issue(hass, entry)
    finally:
        proceed.set()
        entry.setup_lock.release()
        await hass.async_block_till_done()
    assert old_client.closed
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.email == "new@example.test"
    await entry.runtime_data.set_swing(OffOnStatus.ON)
    assert len(posts(cloud)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("disable", [False, True])
async def test_queued_command_is_rejected_during_unload(
    hass, entry, cloud, provider, disable
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    started, proceed = asyncio.Event(), asyncio.Event()
    original_unload = hass.config_entries.async_unload_platforms

    async def pause_unload(config_entry, platforms):
        started.set()
        await proceed.wait()
        return await original_unload(config_entry, platforms)

    lock = identity.binding_lock(hass)
    await lock.acquire()
    service = unload = None
    try:
        with patch.object(hass.config_entries, "async_unload_platforms", pause_unload):
            service = asyncio.create_task(
                hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": swing_id(hass, entry)},
                    blocking=True,
                )
            )
            await asyncio.sleep(0)
            if disable:
                unload = asyncio.create_task(
                    hass.config_entries.async_set_disabled_by(
                        entry.entry_id, ConfigEntryDisabler.USER
                    )
                )
            else:
                unload = asyncio.create_task(
                    hass.config_entries.async_unload(entry.entry_id)
                )
            await asyncio.wait_for(started.wait(), timeout=5)
            lock.release()
            with pytest.raises(HomeAssistantError):
                await service
            assert not posts(cloud)
            assert not binding_issue(hass, entry)
    finally:
        if lock.locked():
            lock.release()
        proceed.set()
        if unload:
            await unload
        if service and not service.done():
            await service


@pytest.mark.parametrize("phase", ["prefetch", "prefetch_auth", "forward"])
async def test_setup_retries_if_settings_change_during_await(
    hass, entry, cloud, provider, phase
):
    started, proceed = asyncio.Event(), asyncio.Event()
    original_forward = hass.config_entries.async_forward_entry_setups
    old_clients = []
    forwarded_entities = []

    async def properties(client):
        if client.email == "old@example.test" and phase.startswith("prefetch"):
            old_clients.append(client)
            started.set()
            await proceed.wait()
            if phase == "prefetch_auth":
                raise InvalidCredentialsError("old credentials rejected")
        return PROPERTIES

    async def forward(config_entry, platforms):
        await original_forward(config_entry, platforms)
        forwarded_entities.append(platform_entities(hass))
        if config_entry.runtime_data.email == "old@example.test" and phase == "forward":
            old_clients.append(config_entry.runtime_data)
            started.set()
            await proceed.wait()

    with (
        patch.object(APIClient, "get_properties", properties),
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
    ):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry,
                options={CONF_EMAIL: "new@example.test", CONF_PASSWORD: "new-secret"},
            )
            proceed.set()
            assert not await setup
            assert entry.state is ConfigEntryState.SETUP_RETRY
            assert old_clients and old_clients[0]._closed
            assert not binding_issue(hass, entry)
            assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data.email == "new@example.test"
            assert all(forwarded_entities[-1].values())
            assert platform_entities(hass) == forwarded_entities[-1]
            if phase == "forward":
                assert len(forwarded_entities) == 2
                assert forwarded_entities[0] == forwarded_entities[1]
            assert not posts(cloud)
            await entry.runtime_data.set_swing(OffOnStatus.ON)
            assert len(posts(cloud)) == 1
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert not any(platform_entities(hass).values())
        finally:
            proceed.set()
            if not setup.done():
                await setup


async def test_same_effective_settings_allow_a_queued_command(
    hass, entry, cloud, provider
):
    """Changes hidden by an options override do not invalidate the client."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_EMAIL: "override@example.test"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    started, proceed = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def gate(func, *args):
        if func is identity._read_journal:
            started.set()
            await proceed.wait()
        return await original(func, *args)

    await entry.setup_lock.acquire()
    try:
        with patch.object(hass, "async_add_executor_job", gate):
            task = asyncio.create_task(
                hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": swing_id(hass, entry)},
                    blocking=True,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_EMAIL: "unused@example.test"}
            )
            proceed.set()
            await task
            assert len(posts(cloud)) == 1
            assert not binding_issue(hass, entry)
    finally:
        proceed.set()
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("stale", ["settings", "closed", "replaced"])
async def test_stale_runtime_during_journal_failure_creates_no_repair(
    hass, entry, cloud, provider, stale
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    started, proceed = asyncio.Event(), asyncio.Event()
    original = hass.async_add_executor_job

    async def failed_read(func, *args):
        if func is identity._read_journal:
            started.set()
            await proceed.wait()
            raise identity.ClaimJournalError("private cloud details")
        return await original(func, *args)

    await entry.setup_lock.acquire()
    try:
        with patch.object(hass, "async_add_executor_job", failed_read):
            task = asyncio.create_task(client.set_swing(OffOnStatus.ON))
            await asyncio.wait_for(started.wait(), timeout=5)
            if stale == "settings":
                hass.config_entries.async_update_entry(
                    entry, options={CONF_PASSWORD: "new-secret"}
                )
            elif stale == "closed":
                await client.async_shutdown()
            else:
                entry.runtime_data = APIClient(
                    None, "en", "old@example.test", "old-secret"
                )
            proceed.set()
            with pytest.raises(HomeAssistantError) as error:
                await task
            assert "private" not in str(error.value)
            assert not posts(cloud)
            assert not binding_issue(hass, entry)
            entry.runtime_data = client
    finally:
        proceed.set()
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_current_runtime_with_corrupt_journal_creates_repair(
    hass, entry, cloud, provider
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    with (
        patch.object(
            identity, "_read_journal", side_effect=identity.ClaimJournalError("private")
        ),
        pytest.raises(HomeAssistantError, match="ownership"),
    ):
        await entry.runtime_data.set_swing(OffOnStatus.ON)
    assert not posts(cloud)
    assert binding_issue(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_an_override_fences_old_client(hass, entry, cloud, provider):
    hass.config_entries.async_update_entry(
        entry, options={CONF_PASSWORD: "override-secret"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    await entry.setup_lock.acquire()
    try:
        hass.config_entries.async_update_entry(entry, options={})
        with pytest.raises(HomeAssistantError):
            await client.set_swing(OffOnStatus.ON)
        assert not posts(cloud)
        assert not binding_issue(hass, entry)
    finally:
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_runtime_reauth_is_not_started(hass, entry, cloud, provider):
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    await entry.setup_lock.acquire()
    try:
        hass.config_entries.async_update_entry(
            entry, options={CONF_EMAIL: "new@example.test"}
        )
        client._report_invalid_credentials()
        await asyncio.sleep(0)
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert not binding_issue(hass, entry)
    finally:
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_queued_runtime_reauth_is_cancelled_after_settings_change(
    hass, entry, cloud, provider
):
    from custom_components.delonghi_dehumidifier_api.const import RUNTIME_REAUTH_ID

    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    flow_finished = asyncio.Event()
    original_init = hass.config_entries.flow.async_init

    async def observe_init(*args, **kwargs):
        result = await original_init(*args, **kwargs)
        flow_finished.set()
        return result

    await entry.setup_lock.acquire()
    await entry._reauth_lock.acquire()
    try:
        with patch.object(hass.config_entries.flow, "async_init", observe_init):
            entry.async_start_reauth(hass, data={RUNTIME_REAUTH_ID: client.reauth_id})
            hass.config_entries.async_update_entry(
                entry, options={CONF_EMAIL: "new@example.test"}
            )
            entry._reauth_lock.release()
            await asyncio.wait_for(flow_finished.wait(), timeout=5)
            assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            assert not binding_issue(hass, entry)
    finally:
        if entry._reauth_lock.locked():
            entry._reauth_lock.release()
        entry.setup_lock.release()
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


async def test_inflight_old_token_rejection_does_not_start_reauth(hass, loaded_entry):
    entry, session, _ = loaded_entry
    old_client = entry.runtime_data
    session.pause = True
    request = asyncio.create_task(old_client.get_access_token())
    await asyncio.wait_for(session.login_started.wait(), timeout=5)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ):
        hass.config_entries.async_update_entry(
            entry, options={CONF_EMAIL: "new@example.test"}
        )
        session.continue_login.set()
        with pytest.raises(InvalidCredentialsError):
            await request
        await hass.async_block_till_done()
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert not binding_issue(hass, entry)


@pytest.mark.parametrize("failure", ["false", "exception", "cancel"])
async def test_failed_unload_requires_restart_without_repeating_cleanup(
    hass, entry, cloud, provider, failure, caplog
):
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    original_unload = hass.config_entries.async_unload_platforms
    if failure == "false":
        failed_unload = AsyncMock(return_value=False)
    elif failure == "exception":
        failed_unload = AsyncMock(side_effect=RuntimeError("private-credential"))
    else:
        failed_unload = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        with patch.object(hass.config_entries, "async_unload_platforms", failed_unload):
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await hass.config_entries.async_unload(entry.entry_id)
            else:
                assert not await hass.config_entries.async_unload(entry.entry_id)
            assert client.closed
            assert entry.entry_id in identity.restart_required_entries(hass)
            with pytest.raises(HomeAssistantError):
                await client.set_swing(OffOnStatus.ON)
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            except HomeAssistantError:
                pass
            hass.config_entries.async_update_entry(entry, options={CONF_LANGUAGE: "de"})
            await hass.async_block_till_done()
            assert failed_unload.await_count == 1
            assert not posts(cloud)
            assert "private-credential" not in caplog.text
    finally:
        await original_unload(
            entry, ["binary_sensor", "humidifier", "sensor", "switch"]
        )
        await client.async_shutdown()


async def test_partial_unload_fences_queued_and_manual_reloads(
    hass, entry, cloud, provider
):
    from homeassistant.config_entries import OperationNotAllowed

    assert await hass.config_entries.async_setup(entry.entry_id)
    client = entry.runtime_data
    before = platform_entities(hass)
    assert all(before.values())
    alarm_id = next(iter(before["binary_sensor"]))
    humidifier_id = next(iter(before["humidifier"]))
    assert hass.states.get(alarm_id).state in ("on", "off")
    assert hass.states.get(humidifier_id).state != "unavailable"
    assert client._auth_listeners
    original_unload = hass.config_entries.async_unload_platforms
    original_forward = hass.config_entries.async_forward_entry_setups
    partially_unloaded, resume = asyncio.Event(), asyncio.Event()

    async def partial_unload(config_entry, platforms):
        assert await original_unload(config_entry, ["switch"])
        partially_unloaded.set()
        await resume.wait()
        return False

    async def queued_reload():
        try:
            return await hass.config_entries.async_reload(entry.entry_id)
        except OperationNotAllowed:
            return False

    try:
        with (
            patch.object(
                hass.config_entries,
                "async_unload_platforms",
                AsyncMock(side_effect=partial_unload),
            ) as unload,
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(side_effect=original_forward),
            ) as forward,
        ):
            first = asyncio.create_task(
                hass.config_entries.async_unload(entry.entry_id)
            )
            try:
                await asyncio.wait_for(partially_unloaded.wait(), timeout=5)
                after_partial = platform_entities(hass)
                assert not after_partial["switch"]
                assert all(
                    after_partial[domain] == before[domain]
                    for domain in before
                    if domain != "switch"
                )
                queued = asyncio.create_task(queued_reload())
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, identity.CONF_DSN: "changed-device"},
                    options={CONF_LANGUAGE: "de"},
                )
                await asyncio.sleep(0)
                resume.set()
                assert not await first
                assert not await queued
                await hass.async_block_till_done()
                assert not await queued_reload()
                assert entry.entry_id in identity.restart_required_entries(hass)
                assert identity.device_needs_restart(hass, "selected")
                assert not identity.device_needs_restart(hass, "changed-device")
                assert client.closed
                assert hass.states.get(alarm_id).state == "unavailable"
                assert hass.states.get(humidifier_id).state == "unavailable"
                assert not client._auth_listeners
                with pytest.raises(HomeAssistantError):
                    await client.set_swing(OffOnStatus.ON)
                assert unload.await_count == 1
                assert forward.await_count == 0
                assert platform_entities(hass) == after_partial
                assert not posts(cloud)
            finally:
                resume.set()
                if not first.done():
                    await first
    finally:
        await original_unload(entry, ["binary_sensor", "humidifier", "sensor"])
        await client.async_shutdown()


async def test_cancelled_partial_forward_fences_setup_until_restart(
    hass, entry, cloud, provider
):
    original_forward = hass.config_entries.async_forward_entry_setups
    original_unload = hass.config_entries.async_unload_platforms
    started = asyncio.Event()
    clients = []

    async def partial_forward(config_entry, platforms):
        clients.append(config_entry.runtime_data)
        await original_forward(config_entry, ["switch"])
        started.set()
        await asyncio.Event().wait()

    try:
        with patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=partial_forward),
        ) as forward:
            setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
            await asyncio.wait_for(started.wait(), timeout=5)
            setup.cancel()
            try:
                assert not await setup
            except asyncio.CancelledError:
                pass
            assert clients[0].closed
            assert entry.entry_id in identity.restart_required_entries(hass)
            try:
                assert not await hass.config_entries.async_reload(entry.entry_id)
            except HomeAssistantError:
                pass
            assert forward.await_count == 1
            assert not posts(cloud)
    finally:
        await original_unload(entry, ["switch"])
        if clients:
            await clients[0].async_shutdown()


async def test_abnormal_forwarding_is_fenced_until_restart(
    hass, entry, cloud, provider, caplog
):
    original_forward = hass.config_entries.async_forward_entry_setups
    original_unload = hass.config_entries.async_unload_platforms
    clients = []

    async def partial_forward(config_entry, platforms):
        clients.append(config_entry.runtime_data)
        await original_forward(config_entry, platforms)
        raise RuntimeError("private-credential")

    try:
        with patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=partial_forward),
        ) as forward:
            assert not await hass.config_entries.async_setup(entry.entry_id)
            assert clients and clients[0].closed
            assert entry.entry_id in identity.restart_required_entries(hass)
            with pytest.raises(HomeAssistantError):
                await clients[0].set_swing(OffOnStatus.ON)
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            except HomeAssistantError:
                pass
            assert forward.call_count == 1
            assert not posts(cloud)
            assert "private-credential" not in caplog.text
    finally:
        await original_unload(
            entry, ["binary_sensor", "humidifier", "sensor", "switch"]
        )
        if clients:
            await clients[0].async_shutdown()


async def test_setup_retry_uses_settings_changed_again_during_cleanup(
    hass, entry, cloud, provider
):
    forwarded, continue_forward, cleanup_started, continue_cleanup = (
        asyncio.Event() for _ in range(4)
    )
    original_forward = hass.config_entries.async_forward_entry_setups
    original_unload = hass.config_entries.async_unload_platforms

    async def forward(config_entry, platforms):
        await original_forward(config_entry, platforms)
        if config_entry.runtime_data.email == "old@example.test":
            config_entry.runtime_data._invalid_credentials = True
            forwarded.set()
            await continue_forward.wait()

    async def unload(config_entry, platforms):
        cleanup_started.set()
        await continue_cleanup.wait()
        return await original_unload(config_entry, platforms)

    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
        patch.object(hass.config_entries, "async_unload_platforms", unload),
    ):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(forwarded.wait(), timeout=5)
            old_client = entry.runtime_data
            hass.config_entries.async_update_entry(
                entry, options={CONF_EMAIL: "new@example.test"}
            )
            continue_forward.set()
            await asyncio.wait_for(cleanup_started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry, options={CONF_EMAIL: "latest@example.test"}
            )
            continue_cleanup.set()
            assert not await setup
            assert entry.state is ConfigEntryState.SETUP_RETRY
            assert old_client.closed
            assert not binding_issue(hass, entry)
            assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data.email == "latest@example.test"
            assert await hass.config_entries.async_unload(entry.entry_id)
        finally:
            continue_forward.set()
            continue_cleanup.set()
            if not setup.done():
                await setup


@pytest.mark.parametrize("failure", ["false", "exception", "cancel"])
async def test_failed_setup_cleanup_blocks_repeated_forwarding(
    hass, entry, cloud, provider, failure, caplog
):
    original_forward = hass.config_entries.async_forward_entry_setups
    original_unload = hass.config_entries.async_unload_platforms
    clients = []

    async def forward(config_entry, platforms):
        clients.append(config_entry.runtime_data)
        await original_forward(config_entry, platforms)
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_EMAIL: "new@example.test"}
        )

    if failure == "false":
        failed_unload = AsyncMock(return_value=False)
    elif failure == "exception":
        failed_unload = AsyncMock(side_effect=RuntimeError("private-credential"))
    else:
        failed_unload = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        with (
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(side_effect=forward),
            ) as forwarded,
            patch.object(hass.config_entries, "async_unload_platforms", failed_unload),
        ):
            assert not await hass.config_entries.async_setup(entry.entry_id)
            assert clients and clients[0].closed
            assert entry.entry_id in identity.restart_required_entries(hass)
            with pytest.raises(HomeAssistantError):
                await clients[0].set_swing(OffOnStatus.ON)
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            except HomeAssistantError:
                pass
            assert forwarded.await_count == 1
            assert failed_unload.await_count == 1
            assert not posts(cloud)
            assert not binding_issue(hass, entry)
            assert "private-credential" not in caplog.text
    finally:
        await original_unload(
            entry, ["binary_sensor", "humidifier", "sensor", "switch"]
        )
        if clients:
            await clients[0].async_shutdown()


async def test_settings_change_during_election_does_not_lose_claim(
    hass, cloud, provider
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.delonghi_dehumidifier_api import save_election
    from custom_components.delonghi_dehumidifier_api.identity import (
        CONF_DSN,
        CONF_ENROLLMENT,
        device_unique_id,
        reserve_enrollment,
    )

    token = reserve_enrollment(hass, "selected")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=device_unique_id("selected"),
        data={
            CONF_EMAIL: "old@example.test",
            CONF_PASSWORD: "old-secret",
            CONF_LANGUAGE: "en",
            CONF_DSN: "selected",
            CONF_ENROLLMENT: token,
        },
    )
    entry.add_to_hass(hass)
    saved, proceed = asyncio.Event(), asyncio.Event()

    async def gated_save(*args):
        await save_election(*args)
        saved.set()
        await proceed.wait()

    with patch("custom_components.delonghi_dehumidifier_api.save_election", gated_save):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            await asyncio.wait_for(saved.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry, options={CONF_EMAIL: "new@example.test"}
            )
            proceed.set()
            assert not await setup
            assert entry.state is ConfigEntryState.SETUP_RETRY
            assert not binding_issue(hass, entry)
            assert not posts(cloud)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert entry.state is ConfigEntryState.LOADED
            assert entry.runtime_data.email == "new@example.test"
            assert CONF_ENROLLMENT not in entry.data
            assert await hass.config_entries.async_unload(entry.entry_id)
        finally:
            proceed.set()
            if not setup.done():
                await setup


async def test_failed_unload_does_not_block_another_entry(hass, entry, cloud, provider):
    assert await hass.config_entries.async_setup(entry.entry_id)
    first_client = entry.runtime_data
    original_unload = hass.config_entries.async_unload_platforms
    other = bound_entry(domain=DOMAIN, dsn="other", data=dict(entry.data))
    other.add_to_hass(hass)
    try:
        with patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        ):
            assert not await hass.config_entries.async_unload(entry.entry_id)
        with patch.object(
            APIClient, "get_devices", AsyncMock(return_value=["selected", "other"])
        ):
            assert await hass.config_entries.async_setup(other.entry_id)
            await other.runtime_data.set_swing(OffOnStatus.ON)
            assert len(posts(cloud)) == 1
            assert "/dsns/other/" in posts(cloud)[0][1]
            assert entry.entry_id in identity.restart_required_entries(hass)
            assert other.entry_id not in identity.restart_required_entries(hass)
            assert await hass.config_entries.async_unload(other.entry_id)
    finally:
        await original_unload(
            entry, ["binary_sensor", "humidifier", "sensor", "switch"]
        )
        await first_client.async_shutdown()


async def test_restart_clears_failed_unload_fence(tmp_path):
    from tests.test_identity_restart_rollback import (
        DATA,
        FakeCloud,
        fresh_home_assistant,
        save_home_assistant,
    )

    cloud = FakeCloud()
    async with cloud.patched():
        async with fresh_home_assistant(tmp_path) as hass:
            entry = bound_entry(domain=DOMAIN, dsn="a", data=DATA)
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await save_home_assistant(hass)
            entry_id = entry.entry_id
            original_unload = hass.config_entries.async_unload_platforms
            with patch.object(
                hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=False),
            ):
                assert not await hass.config_entries.async_unload(entry_id)
            assert entry_id in identity.restart_required_entries(hass)
            assert entry.runtime_data.closed
            await original_unload(
                entry, ["binary_sensor", "humidifier", "sensor", "switch"]
            )
        async with fresh_home_assistant(tmp_path) as hass:
            entry = hass.config_entries.async_get_entry(entry_id)
            assert entry_id not in identity.restart_required_entries(hass)
            assert await hass.config_entries.async_setup(entry_id)
            await entry.runtime_data.set_swing(OffOnStatus.ON)
            assert len(cloud.posts) == 1
