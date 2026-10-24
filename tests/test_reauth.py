"""Test setup-time reauthentication through Home Assistant's real entry flow."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.data_entry_flow import UnknownFlow

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests.identity_helpers import bound_entry as MockConfigEntry

DATA = {
    "device_dsn": "test-dsn",
    CONF_EMAIL: "original@example.com",
    CONF_LANGUAGE: "en",
    CONF_PASSWORD: "bad-password",
}
OPTIONS = {
    CONF_EMAIL: "effective@example.com",
    CONF_LANGUAGE: "pt",
    CONF_PASSWORD: "bad-password",
    "keep_option": "unchanged",
}


class ProviderSession:
    """Synthetic provider exercising the real client authentication sequence."""

    def __init__(self, *, connection_error=False):
        self.logins = []
        self.connection_error = connection_error
        self.empty_access_token = False
        self.paused_password = None
        self.login_started = asyncio.Event()
        self.continue_login = asyncio.Event()

    async def get(self, url, **kwargs):
        if self.connection_error:
            raise aiohttp.ClientConnectionError("provider unreachable")
        if url.endswith("/authorize"):
            return SimpleNamespace(
                status=302,
                headers={"Location": "https://example.invalid/?context=test"},
            )
        if url.endswith("/socialize.getIDs"):
            return self.json_response(
                {"ucid": "ucid", "gmid": "gmid", "gmidTicket": "ticket"}
            )
        if url.endswith("/OIDCConsentPage.php"):
            return SimpleNamespace(
                status=200,
                text=AsyncMock(return_value="const consentObj2Sig = 'signature';"),
            )
        if url.endswith("/authorize/continue"):
            return SimpleNamespace(
                status=302, headers={"Location": "https://example.invalid/?code=test"}
            )
        raise AssertionError(url)

    async def post(self, url, **kwargs):
        if url.endswith("/accounts.login"):
            data = kwargs["data"]
            self.logins.append((data["loginID"], data["lang"], data["password"]))
            if data["password"] == self.paused_password:
                self.login_started.set()
                await self.continue_login.wait()
            error_code = {
                "bad-password": 403042,
                "locked": 403120,
                "server-error": 500001,
            }.get(data["password"])
            if error_code:
                return self.json_response({"errorCode": error_code})
            return self.json_response(
                {"errorCode": 0, "sessionInfo": {"login_token": "token"}}
            )
        if url.endswith("/socialize.getUserInfo"):
            return self.json_response(
                {"UID": "uid", "UIDSignature": "signature", "signatureTimestamp": "123"}
            )
        if url.endswith("/token"):
            return self.json_response({"access_token": "idp-token"})
        if url.endswith("/token_sign_in"):
            return self.json_response(
                {
                    "access_token": "" if self.empty_access_token else "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                }
            )
        raise AssertionError(url)

    @staticmethod
    def json_response(data):
        return SimpleNamespace(
            status=200, text=AsyncMock(return_value=json.dumps(data))
        )


@pytest.fixture
def provider():
    session = ProviderSession()
    values = {
        "appliance_model": "model",
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
    properties = [{"product_name": "Test"}] + [
        {"name": name, "value": value} for name, value in values.items()
    ]
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=session,
        ),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=properties)),
    ):
        yield session


async def get_reauth_form(hass, entry):
    """Get the existing entry's pending flow and its password-only form."""
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    with pytest.raises(vol.Invalid):
        result["data_schema"]({})
    assert result["data_schema"]({CONF_PASSWORD: "entered-password"}) == {
        CONF_PASSWORD: "entered-password"
    }
    return result


async def setup_with_invalid_credentials(hass, options):
    """Set up an entry that must request reauthentication before forwarding."""
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not entry.update_listeners
    return entry


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("bad-password", "invalid_auth"),
        ("locked", "unknown"),
        ("server-error", "unknown"),
    ],
)
async def test_failed_reauth_preserves_settings(hass, provider, password, expected):
    entry = await setup_with_invalid_credentials(hass, OPTIONS)
    result = await get_reauth_form(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: password}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == expected
    assert entry.data == DATA
    assert entry.options == OPTIONS
    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize("options", [{}, OPTIONS])
async def test_invalid_setup_reauth_and_recovery(hass, provider, options):
    """Failed setup creates a flow and success reloads with effective credentials."""
    entry = await setup_with_invalid_credentials(hass, options)
    result = await get_reauth_form(hass, entry)
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )
        await hass.async_block_till_done()
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert reload_entry.await_count == 1
    assert entry.state is ConfigEntryState.LOADED
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data == {**DATA, CONF_PASSWORD: "new-password"}
    assert entry.options == {
        key: value for key, value in options.items() if key != CONF_PASSWORD
    }
    effective = {**DATA, **options, CONF_PASSWORD: "new-password"}
    assert (
        entry.runtime_data.email,
        entry.runtime_data.language,
        entry.runtime_data.password,
    ) == (effective[CONF_EMAIL], effective[CONF_LANGUAGE], effective[CONF_PASSWORD])
    assert (
        provider.logins[-2:]
        == [(effective[CONF_EMAIL], effective[CONF_LANGUAGE], "new-password")] * 2
    )
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not entry.update_listeners


@pytest.mark.parametrize("same_password", [False, True])
async def test_loaded_entry_reauth_reloads_once(hass, provider, same_password):
    """A listener reloads changed entries; unchanged entries still need a reload."""
    options = {**OPTIONS, CONF_PASSWORD: "good-password"}
    data = DATA
    if same_password:
        data = {**DATA, CONF_PASSWORD: "good-password"}
        options = {key: value for key, value in OPTIONS.items() if key != CONF_PASSWORD}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    original_client = entry.runtime_data
    entry.async_start_reauth(hass)
    result = await get_reauth_form(hass, entry)
    password = "good-password" if same_password else "new-password"
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: password}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert reload_entry.await_count == 1
    assert entry.runtime_data is not original_client
    assert entry.runtime_data.email == OPTIONS[CONF_EMAIL]
    assert entry.runtime_data.language == OPTIONS[CONF_LANGUAGE]
    assert entry.runtime_data.password == password
    assert entry.options == {k: v for k, v in OPTIONS.items() if k != CONF_PASSWORD}
    assert len(entry.update_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("password", ["locked", "server-error"])
async def test_setup_non_auth_provider_errors_do_not_start_reauth(
    hass, provider, password
):
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, CONF_PASSWORD: password})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_setup_network_error_is_retryable_without_reauth(hass, provider):
    provider.connection_error = True
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    entry.async_cancel_retry_setup()


async def test_setup_empty_access_token_is_retryable_without_reauth(hass, provider):
    provider.empty_access_token = True
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, CONF_PASSWORD: "good"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    entry.async_cancel_retry_setup()


@pytest.mark.parametrize(
    ("provider_failure", "expected"),
    [("connection_error", "cannot_connect"), ("empty_access_token", "unknown")],
)
async def test_reauth_does_not_save_unconfirmed_credentials(
    hass, provider, provider_failure, expected
):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=OPTIONS)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    result = await get_reauth_form(hass, entry)
    setattr(provider, provider_failure, True)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == expected
    assert entry.data == DATA
    assert entry.options == OPTIONS
    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize("loaded", [False, True])
async def test_reauth_does_not_overwrite_concurrent_settings_change(
    hass, provider, loaded
):
    """A password validated for one account cannot overwrite newer settings."""
    options = {**OPTIONS, CONF_PASSWORD: "good"} if loaded else OPTIONS
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is loaded
    if loaded:
        entry.async_start_reauth(hass)
    form = await get_reauth_form(hass, entry)
    provider.paused_password = "candidate-password"
    task = asyncio.create_task(
        hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: "candidate-password"}
        )
    )
    await asyncio.wait_for(provider.login_started.wait(), timeout=5)
    newer_options = {
        **options,
        CONF_EMAIL: "newer@example.com",
        CONF_PASSWORD: "newer-password",
    }
    hass.config_entries.async_update_entry(entry, options=newer_options)
    await hass.async_block_till_done()
    provider.continue_login.set()
    result = await task
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_settings_changed"
    assert entry.data == DATA
    assert entry.options == newer_options
    if loaded:
        assert entry.runtime_data.email == "newer@example.com"
        assert entry.runtime_data.password == "newer-password"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cancelled_reauth_does_not_apply_in_flight_password(hass, provider):
    options = {**OPTIONS, CONF_PASSWORD: "good"}
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    entry.async_start_reauth(hass)
    form = await get_reauth_form(hass, entry)
    provider.paused_password = "candidate-password"
    task = asyncio.create_task(
        hass.config_entries.flow.async_configure(
            form["flow_id"], user_input={CONF_PASSWORD: "candidate-password"}
        )
    )
    await asyncio.wait_for(provider.login_started.wait(), timeout=5)
    hass.config_entries.flow.async_abort(form["flow_id"])
    provider.continue_login.set()
    try:
        result = await task
    except UnknownFlow:
        pass  # Older Home Assistant versions report that the flow was removed.
    else:
        assert result["type"] == "abort"
    await hass.async_block_till_done()
    assert entry.data == DATA
    assert entry.options == options
    assert entry.runtime_data.password == "good"
    assert await hass.config_entries.async_unload(entry.entry_id)
