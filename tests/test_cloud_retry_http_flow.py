"""Exercise retry from actual HTTP status handling through Home Assistant setup."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.utils import fetch_device_info


class FakeRequest:
    """Support both await and async-with forms used by aiohttp requests."""

    def __init__(self, response):
        self.response = response

    def __await__(self):
        async def response():
            return self.response

        return response().__await__()

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return None


class FakeCloud:
    """Answer the actual authentication sequence and discovery locally."""

    def __init__(self, status, phase):
        self.phase = phase
        self.recovered = False
        self.failure = Mock(status=status, text=AsyncMock(), json=AsyncMock())
        self.requests = []

    @staticmethod
    def response(*, body=None, location=None):
        return Mock(
            status=200,
            headers={"Location": location} if location else {},
            text=AsyncMock(
                return_value=body if isinstance(body, str) else json.dumps(body)
            ),
            json=AsyncMock(return_value=body),
        )

    def phase_response(self, phase, **kwargs):
        """Fail the selected phase until the cloud recovers."""
        if self.phase == phase and not self.recovered:
            return self.failure
        return self.response(**kwargs)

    def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        if url.endswith("/dsns/device-a/properties.json"):
            response = self.phase_response(
                "properties",
                body=[
                    {"property": {"product_name": "Test"}},
                    {"property": {"name": "appliance_model", "value": "DDSX220"}},
                    {"property": {"name": "current_humidity", "value": 50}},
                    {"property": {"name": "alarm_state", "value": 0}},
                    {"property": {"name": "device_status", "value": 0}},
                    {"property": {"name": "device_mode", "value": 1}},
                    {"property": {"name": "swing", "value": 0}},
                    {"property": {"name": "set_eco", "value": 0}},
                ],
            )
        elif "ads-eu.aylanetworks.com/apiv1/devices.json" in url:
            response = self.phase_response(
                "discovery", body=[{"device": {"dsn": "device-a"}}]
            )
        elif "/authorize/continue" in url:
            response = self.response(location="https://example.test/?code=code")
        elif "/authorize" in url:
            response = self.phase_response(
                "authenticate", location="https://example.test/?context=context"
            )
        elif "socialize.getIDs" in url:
            response = self.response(body={"ucid": "u", "gmid": "g", "gmidTicket": "t"})
        elif "OIDCConsentPage.php" in url:
            response = self.response(body="const consentObj2Sig = 'signature';")
        else:
            raise AssertionError(f"Unexpected GET: {url}")
        return FakeRequest(response)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url))
        if "/dsns/" in url:
            raise AssertionError("Unexpected device command during setup")
        if "accounts.login" in url:
            body = {"sessionInfo": {"login_token": "login"}}
        elif "socialize.getUserInfo" in url:
            body = {"UID": "u", "UIDSignature": "s", "signatureTimestamp": "t"}
        elif url.endswith("/token"):
            body = {"access_token": "idp"}
        elif url.endswith("/token_sign_in"):
            body = {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            }
        else:
            raise AssertionError(f"Unexpected POST: {url}")
        return FakeRequest(self.response(body=body))


@pytest.mark.parametrize("status", [429, 503])
async def test_property_http_failure_retries_before_adding_entities(hass, status):
    """A transient properties response retries the entry, then loads all platforms."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LANGUAGE: "en",
            CONF_EMAIL: "user@example.test",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)
    cloud = FakeCloud(status, "properties")
    with patch(
        "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
        return_value=cloud,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        cloud.failure.release.assert_called_once()
        cloud.recovered = True
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        registry = er.async_get(hass)
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert {entity.domain for entity in entities} == {
            "binary_sensor",
            "humidifier",
            "sensor",
            "switch",
        }
        humidity = next(
            entity.entity_id
            for entity in entities
            if entity.unique_id.endswith("_current_humidity_sensor")
        )
        assert hass.states.get(humidity).state == "50"
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert not any(
        method == "POST" and "/dsns/" in url for method, url in cloud.requests
    )


async def test_platform_retry_after_initial_properties_expire(hass):
    """An outage after prefetch still retries platforms instead of losing them."""
    await hass.async_start()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LANGUAGE: "en",
            CONF_EMAIL: "user@example.test",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)
    cloud = FakeCloud(503, "properties")
    cloud.recovered = True
    forward = hass.config_entries.async_forward_entry_setups

    async def forwarding_after_cache_expiry(entry, platforms):
        entry.runtime_data.invalidate_properties()
        cloud.recovered = False
        await forward(entry, platforms)

    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
            return_value=cloud,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            forwarding_after_cache_expiry,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        cloud.recovered = True
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done(wait_background_tasks=True)
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert {entity.domain for entity in entities} == {
            "binary_sensor",
            "humidifier",
            "sensor",
            "switch",
        }
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert not any(
        method == "POST" and "/dsns/" in url for method, url in cloud.requests
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private"),
        aiohttp.ClientConnectionError("private"),
        TransientCloudError(),
    ],
)
async def test_platform_retry_error_does_not_expose_details(error):
    client = Mock(get_product_name=AsyncMock(side_effect=error))
    with pytest.raises(PlatformNotReady, match="Unable to contact DeLonghi") as exc:
        await fetch_device_info(client)
    assert exc.value.__cause__ is error
    assert "private" not in str(exc.value)


async def test_platform_auth_failure_is_not_converted_to_retry():
    error = InvalidCredentialsError("rejected")
    client = Mock(get_product_name=AsyncMock(side_effect=error))
    with pytest.raises(InvalidCredentialsError) as exc:
        await fetch_device_info(client)
    assert exc.value is error


@pytest.mark.parametrize(
    ("error", "state", "reauth"),
    [
        (InvalidCredentialsError("rejected"), ConfigEntryState.SETUP_ERROR, True),
        (TimeoutError(), ConfigEntryState.SETUP_RETRY, False),
    ],
)
async def test_initial_property_failure_cleans_up_and_preserves_auth(
    hass, error, state, reauth
):
    """Property prefetch must close the failed client and classify errors correctly."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LANGUAGE: "en",
            CONF_EMAIL: "user@example.test",
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)
    closed = []
    shutdown = APIClient.async_shutdown

    async def close(client):
        closed.append(client)
        await shutdown(client)

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="device-a")),
        patch.object(APIClient, "get_properties", AsyncMock(side_effect=error)),
        patch.object(APIClient, "async_shutdown", close),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock()
        ) as forward,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is state
        assert (
            bool(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) is reauth
        )
        assert len(closed) == 1 and closed[0]._closed
        forward.assert_not_awaited()
        assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if state is ConfigEntryState.SETUP_RETRY:
            entry.async_cancel_retry_setup()
