"""Privacy and actionable setup failures through real Home Assistant flows."""

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
)

from custom_components.delonghi_dehumidifier_api import async_setup_entry
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import CONF_DSN
from tests.identity_helpers import bound_entry

SECRETS = (
    "secret-email@example.com",
    "secret-password",
    "secret-dsn",
    "secret-token",
    "https://secret.example/path",
    "secret-provider-value",
)
CONFIG = {CONF_EMAIL: SECRETS[0], CONF_PASSWORD: SECRETS[1], CONF_LANGUAGE: "en"}
ROOT = Path(__file__).resolve().parents[1]


def integration_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name.startswith(f"custom_components.{DOMAIN}")
    ]


def assert_safe(records):
    assert records
    for record in records:
        assert not record.exc_info
        assert not record.stack_info
        assert not any(secret in record.getMessage() for secret in SECRETS)


@pytest.mark.parametrize(
    ("authentication", "devices", "expected"),
    [
        (InvalidCredentialsError(SECRETS[0]), None, "invalid_auth"),
        (False, None, "invalid_auth"),
        (TimeoutError(SECRETS[4]), None, "cannot_connect"),
        (aiohttp.ClientConnectionError(SECRETS[4]), None, "cannot_connect"),
        (TransientCloudError(), None, "cannot_connect"),
        (True, [], "no_devices"),
        (True, {}, "unknown"),
        (True, TimeoutError(SECRETS[4]), "cannot_connect"),
        (True, aiohttp.ClientConnectionError(SECRETS[4]), "cannot_connect"),
        (True, TransientCloudError(), "cannot_connect"),
        (True, RuntimeError(SECRETS[5]), "unknown"),
    ],
)
@pytest.mark.parametrize("flow_kind", ["user", "options"])
async def test_flow_failure_classification_and_no_secret_logs(
    hass, caplog, authentication, devices, expected, flow_kind
):
    """Failed forms preserve settings and never log provider exception details."""
    if flow_kind == "user":
        entry = None
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    else:
        entry = bound_entry(
            domain=DOMAIN, data=CONFIG, options={CONF_LANGUAGE: "de"}, dsn=SECRETS[2]
        )
        entry.add_to_hass(hass)
        original_data, original_options = dict(entry.data), dict(entry.options)
        form = await hass.config_entries.options.async_init(entry.entry_id)
    auth = (
        AsyncMock(side_effect=authentication)
        if isinstance(authentication, Exception)
        else AsyncMock(return_value=authentication)
    )
    request = (
        AsyncMock(side_effect=devices)
        if isinstance(devices, Exception)
        else AsyncMock(return_value=devices)
    )
    closed = []
    shutdown = APIClient.async_shutdown

    async def close(client):
        closed.append(client)
        await shutdown(client)

    with (
        caplog.at_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}"),
        patch.object(APIClient, "authenticate", auth),
        patch.object(APIClient, "get_request", request),
        patch.object(APIClient, "async_shutdown", close),
    ):
        if flow_kind == "user":
            result = await hass.config_entries.flow.async_configure(
                form["flow_id"], user_input=CONFIG
            )
        else:
            result = await hass.config_entries.options.async_configure(
                form["flow_id"], user_input=CONFIG
            )
    assert result["type"] == "form"
    assert result["errors"]["base"] == expected
    if entry is not None:
        assert entry.data == original_data and entry.options == original_options
        assert not entry.update_listeners
    assert len(closed) == 1 and closed[0]._closed
    records = integration_records(caplog)
    if expected == "unknown":
        assert_safe(records)
    else:
        assert all(
            not any(secret in record.getMessage() for secret in SECRETS)
            and not record.exc_info
            for record in records
        )


async def test_options_failures_do_not_touch_loaded_client(hass, caplog):
    """A failed option update leaves the actual running entry untouched."""
    entry = bound_entry(
        domain=DOMAIN, data=CONFIG, options={CONF_LANGUAGE: "de"}, dsn=SECRETS[2]
    )
    entry.add_to_hass(hass)
    original_data, original_options = dict(entry.data), dict(entry.options)
    properties = [{"product_name": "Test", "name": "device_status", "value": 1}]
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(
            APIClient,
            "get_request",
            AsyncMock(return_value=[{"device": {"dsn": SECRETS[2]}}]),
        ),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=properties)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client = entry.runtime_data
        listeners = list(entry.update_listeners)
        for error, expected in [
            (TimeoutError(SECRETS[4]), "cannot_connect"),
            (TransientCloudError(), "cannot_connect"),
            (InvalidCredentialsError(SECRETS[0]), "invalid_auth"),
            (RuntimeError(SECRETS[5]), "unknown"),
        ]:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            with patch.object(APIClient, "authenticate", AsyncMock(side_effect=error)):
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], user_input=CONFIG
                )
            assert result["errors"]["base"] == expected
            assert entry.data == original_data and entry.options == original_options
            assert (
                entry.runtime_data is client
                and list(entry.update_listeners) == listeners
            )
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with patch.object(APIClient, "get_request", AsyncMock(return_value=[])):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input=CONFIG
            )
        assert result["errors"]["base"] == "no_devices"
        assert entry.data == original_data and entry.options == original_options
        assert (
            entry.runtime_data is client and list(entry.update_listeners) == listeners
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_empty_account_retries_through_home_assistant(hass):
    """Discovery failure forwards nothing; HA retries and loads exactly once."""
    entry = bound_entry(domain=DOMAIN, data=CONFIG, dsn=SECRETS[2])
    entry.add_to_hass(hass)
    original_data, original_unique_id = dict(entry.data), entry.unique_id
    devices = []
    properties = [
        {"property": {"name": "device_status", "value": 1, "product_name": "Test"}}
    ]
    closed = []
    shutdown = APIClient.async_shutdown

    async def close(client):
        closed.append(client)
        await shutdown(client)

    async def request(path):
        return devices if path == "apiv1/devices.json" else properties

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=request)),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
        patch.object(APIClient, "async_shutdown", close),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            wraps=hass.config_entries.async_forward_entry_setups,
        ) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert len(closed) == 1 and closed[0]._closed
        assert not entry.update_listeners
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        forward.assert_not_awaited()
        post.assert_not_awaited()
        assert entry.data == original_data and entry.unique_id == original_unique_id
        assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        devices.append({"device": {"dsn": SECRETS[2]}})
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        assert len(entry.update_listeners) == 1
        assert len(closed) == 1
        forward.assert_awaited_once()
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert entry.data == original_data and entry.unique_id == original_unique_id
        post.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert len(closed) == 2 and not entry.update_listeners


async def test_cancelled_discovery_closes_client(hass):
    """Cancellation during setup cannot leave a live client or platforms."""
    entry = bound_entry(domain=DOMAIN, data=CONFIG, dsn=SECRETS[2])
    entry.add_to_hass(hass)
    started = asyncio.Event()
    closed = []
    shutdown = APIClient.async_shutdown

    async def close(client):
        closed.append(client)
        await shutdown(client)

    async def discover(_path):
        started.set()
        await asyncio.Event().wait()

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=discover)),
        patch.object(APIClient, "async_shutdown", close),
        patch.object(hass.config_entries, "async_forward_entry_setups") as forward,
    ):
        task = asyncio.create_task(async_setup_entry(hass, entry))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(closed) == 1 and closed[0]._closed
    assert not entry.update_listeners
    forward.assert_not_awaited()


@pytest.mark.parametrize(
    "locale", ["strings.json", "translations/en.json", "translations/de.json"]
)
def test_error_translations_and_sharing_guidance(locale):
    data = json.loads((ROOT / "custom_components" / DOMAIN / locale).read_text())
    for section in ("config", "options"):
        assert {"invalid_auth", "cannot_connect", "no_devices", "unknown"} <= data[
            section
        ]["error"].keys()
    for doc in (ROOT / "README.md", ROOT / ".github/ISSUE_TEMPLATE/issue.md"):
        text = doc.read_text().lower()
        assert all(
            word in text
            for word in (
                "redact",
                "credentials",
                "tokens",
                "identifiers",
                "excerpts",
                "home assistant",
            )
        )
        assert "other components" in text and "chained exceptions" in text


async def test_entity_debug_logs_hide_identifiers_values_and_errors(hass, caplog):
    """Initialization, polls, and actions emit only fixed integration context."""
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers.entity_component import async_update_entity

    entry = bound_entry(domain=DOMAIN, data=CONFIG, dsn=SECRETS[2])
    entry.add_to_hass(hass)
    values = {
        "device_status": 1,
        "device_mode": 1,
        "current_humidity": 54,
        "humidity_setpoint": 55,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "unknown": SECRETS[5],
    }

    async def get(path):
        if path == "apiv1/devices.json":
            return [{"device": {"dsn": SECRETS[2]}}]
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in values.items()
        ]

    with (
        caplog.at_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}"),
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=get)) as request,
        patch.object(APIClient, "post_request", AsyncMock(return_value={})),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)

        def entity(domain, suffix):
            entity_id = registry.async_get_entity_id(
                domain, DOMAIN, f"{DOMAIN}_{SECRETS[2]}_{suffix}"
            )
            return hass.data[domain].get_entity(entity_id)

        humidifier = entity("humidifier", "dehumidifier")
        switch = entity("switch", "eco_mode_switch")
        sensor = entity("sensor", "current_humidity_sensor")
        alarm = entity("binary_sensor", "device_alarm_binary_sensor")
        sensor._attr_name = SECRETS[0]
        switch._attr_name = SECRETS[0]
        entry.runtime_data.access_token = SECRETS[3]
        entry.runtime_data.token_expiry = time.time() + 60
        assert await entry.runtime_data.get_access_token() == SECRETS[3]
        await humidifier.async_turn_on()
        await humidifier.async_turn_off()
        await humidifier.async_set_humidity(55)
        await humidifier.async_set_mode("DEHUMIDIFY")
        with pytest.raises(ServiceValidationError) as exc:
            await humidifier.async_set_mode(SECRETS[5])
        assert SECRETS[5] not in str(exc.value)
        await switch.async_turn_on()
        await switch.async_turn_off()
        sensor._get_value = AsyncMock(return_value=SECRETS[5])
        await sensor.async_update()
        sensor._get_value = entry.runtime_data.get_current_humidity
        request.side_effect = RuntimeError(" ".join(SECRETS))
        entry.runtime_data.device_properties_timestamp = 0
        switch._skip_next_poll = False
        for target in (humidifier, switch, sensor, alarm):
            await async_update_entity(hass, target.entity_id)
        await hass.config_entries.async_unload(entry.entry_id)
    records = integration_records(caplog)
    assert_safe(records)
    messages = " ".join(record.getMessage() for record in records)
    assert "Initialized sensor" in messages
    assert "Updated humidifier" in messages
    assert "Turning on switch" in messages
    assert "Using existing access token" in messages
    assert "Unable to update switch" in messages
    assert "Unable to update device alarm" in messages
    assert "Unable to update entity" in messages


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (InvalidCredentialsError(SECRETS[1]), "invalid_auth"),
        (TimeoutError(SECRETS[4]), "cannot_connect"),
        (aiohttp.ClientConnectionError(SECRETS[4]), "cannot_connect"),
        (TransientCloudError(), "cannot_connect"),
        (RuntimeError(SECRETS[5]), "unknown"),
        (False, "unknown"),
    ],
)
async def test_reauth_error_classification_is_preserved_and_safe(
    hass, caplog, failure, expected
):
    entry = bound_entry(domain=DOMAIN, data=CONFIG, dsn=SECRETS[2])
    entry.add_to_hass(hass)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=CONFIG,
    )
    auth = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    with (
        caplog.at_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}"),
        patch.object(APIClient, "authenticate", auth),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: SECRETS[1]}
        )
    assert result["errors"]["base"] == expected
    assert entry.data == {**CONFIG, CONF_DSN: SECRETS[2]} and not entry.options
    records = integration_records(caplog)
    if isinstance(failure, RuntimeError):
        assert_safe(records)
    assert all(
        not record.exc_info
        and not any(secret in record.getMessage() for secret in SECRETS)
        for record in records
    )


@pytest.mark.parametrize(
    ("authentication", "devices", "expected"),
    [
        (InvalidCredentialsError(SECRETS[0]), None, "invalid_auth"),
        (False, None, "invalid_auth"),
        (TimeoutError(SECRETS[4]), None, "cannot_connect"),
        (aiohttp.ClientConnectionError(SECRETS[4]), None, "cannot_connect"),
        (TransientCloudError(), None, "cannot_connect"),
        (True, [], "no_devices"),
        (True, {}, "unknown"),
        (True, TimeoutError(SECRETS[4]), "cannot_connect"),
        (True, aiohttp.ClientConnectionError(SECRETS[4]), "cannot_connect"),
        (True, TransientCloudError(), "cannot_connect"),
        (True, RuntimeError(SECRETS[5]), "unknown"),
        (True, [{"device": {"dsn": "other-device"}}], "device_not_available"),
    ],
)
async def test_selection_failure_is_private_and_does_not_create_entry(
    hass, caplog, authentication, devices, expected
):
    """The second step rechecks access and safely reports every failure."""
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    first_devices = [
        {"device": {"dsn": SECRETS[2]}},
        {"device": {"dsn": "other-device"}},
    ]
    closed = []
    shutdown = APIClient.async_shutdown

    async def close(client):
        closed.append(client)
        await shutdown(client)

    with (
        caplog.at_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}"),
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)) as auth,
        patch.object(
            APIClient, "get_request", AsyncMock(return_value=first_devices)
        ) as request,
        patch.object(APIClient, "post_request", AsyncMock()) as post,
        patch.object(APIClient, "async_shutdown", close),
    ):
        selection = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input=CONFIG
        )
        assert selection["type"] == "form" and selection["step_id"] == "select_device"
        if isinstance(authentication, Exception):
            auth.side_effect = authentication
        else:
            auth.return_value = authentication
        if isinstance(devices, Exception):
            request.side_effect = devices
        else:
            request.return_value = devices
        result = await hass.config_entries.flow.async_configure(
            selection["flow_id"], user_input={CONF_DSN: SECRETS[2]}
        )
    assert result["type"] == "form" and result["step_id"] == "select_device"
    assert result["errors"]["base"] == expected
    assert not hass.config_entries.async_entries(DOMAIN)
    assert len(closed) == 2 and all(client._closed for client in closed)
    post.assert_not_awaited()
    records = integration_records(caplog)
    if expected == "unknown":
        assert_safe(records)
    assert all(
        not record.exc_info
        and not any(secret in record.getMessage() for secret in SECRETS)
        for record in records
    )


@pytest.mark.parametrize("devices", [[], [{"device": {"dsn": "other-device"}}]])
async def test_reauth_missing_pinned_device_is_private_and_keeps_settings(
    hass, caplog, devices
):
    """Reauthentication verifies the bound device before saving a password."""
    entry = bound_entry(domain=DOMAIN, data=CONFIG, dsn=SECRETS[2])
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=CONFIG,
    )
    with (
        caplog.at_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}"),
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_request", AsyncMock(return_value=devices)),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: SECRETS[1]}
        )
    assert result["errors"]["base"] == "device_not_available"
    assert entry.data == original_data and not entry.options
    post.assert_not_awaited()
    assert all(
        not record.exc_info
        and not any(secret in record.getMessage() for secret in SECRETS)
        for record in integration_records(caplog)
    )
