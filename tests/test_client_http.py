"""Tests for DeLonghi API HTTP responses."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.exceptions import HomeAssistantError

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    OffOnStatus,
    Status,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.switch import GenericOffOnSwitchSensor


class FakeResponse:
    """An HTTP response containing JSON."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.json_calls = 0
        self.released = False

    async def json(self):
        self.json_calls += 1
        return self.body

    def release(self):
        self.released = True


class FakeRequest:
    """An aiohttp request supports awaiting and async context management."""

    def __init__(self, response):
        self.response = response
        self.closed = False

    def __await__(self):
        async def get_response():
            return self.response

        return get_response().__await__()

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        self.closed = True


class FakeSession:
    """Supply one response and record the request."""

    def __init__(self, response):
        self.request = FakeRequest(response)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.request

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.request


def make_client(status, body):
    """Create a client with a synthetic HTTP response and cached device."""
    response = FakeResponse(status, body)
    session = FakeSession(response)
    client = APIClient(session, "en", "user@example.com", "password-secret")
    client.get_access_token = AsyncMock(return_value="access-token-secret")
    client.device_dsn = "device-123"
    return client, session, response


@pytest.mark.asyncio
async def test_rejected_switch_write_does_not_change_state():
    """A JSON error response must not make a rejected write look successful."""
    client, session, _ = make_client(403, {"error": "error-body-secret"})
    switch = GenericOffOnSwitchSensor(
        "device-123",
        {"identifiers": {("delonghi_dehumidifier_api", "device-123")}},
        client,
        "Eco Mode",
        OffOnStatus.OFF,
        client.set_eco,
    )

    error = None
    try:
        await switch.async_turn_on()
    except HomeAssistantError as exc:
        error = exc

    assert switch.is_on is False
    assert error is not None
    assert "403" in str(error)
    assert session.request.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_http_errors_are_safe_and_responses_are_closed(method, status, caplog):
    """Do not return, parse, or expose an unsuccessful response body."""
    client, session, response = make_client(status, {"error": "error-body-secret"})
    caplog.set_level(logging.DEBUG)

    with pytest.raises(HomeAssistantError, match=f"HTTP {status}") as error:
        if method == "GET":
            await client.get_request("secret-url-part")
        else:
            await client.post_request("secret-url-part", {"value": "request-secret"})

    assert response.json_calls == 0
    assert session.request.closed
    for secret in (
        "error-body-secret",
        "access-token-secret",
        "request-secret",
        "secret-url-part",
        "password-secret",
    ):
        assert secret not in str(error.value)
        assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "status"), [("GET", 200), ("POST", 201)])
async def test_successful_requests_return_json_and_close_response(method, status):
    """Keep returning successful JSON bodies for both request methods."""
    body = {"value": "success"}
    client, session, response = make_client(status, body)

    if method == "GET":
        result = await client.get_request("path")
    else:
        result = await client.post_request("path", {"value": 1})

    assert result == body
    assert response.json_calls == 1
    assert session.request.closed


@pytest.mark.asyncio
async def test_successful_switch_write_changes_state():
    """An accepted switch write keeps its existing behavior."""
    client, session, _ = make_client(200, {"value": 1})
    switch = GenericOffOnSwitchSensor(
        "device-123",
        {"identifiers": {("delonghi_dehumidifier_api", "device-123")}},
        client,
        "Eco Mode",
        OffOnStatus.OFF,
        client.set_eco,
    )

    await switch.async_turn_on()

    assert switch.is_on is True
    assert session.request.closed


@pytest.mark.parametrize("status", [429, 500, 503, 599])
async def test_transient_http_statuses_are_distinguishable(status):
    """Retryable provider failures have a safe, typed exception."""
    client, _, response = make_client(status, {"error": "provider-secret"})
    with pytest.raises(TransientCloudError, match=f"HTTP {status}") as error:
        await client.get_request("secret-url-part")
    assert "provider-secret" not in str(error.value)
    assert "secret-url-part" not in str(error.value)
    assert response.json_calls == 0
    assert response.released


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 499])
async def test_other_http_errors_are_not_classified_as_transient(status):
    """Authentication and other nonretryable errors retain their semantics."""
    client, _, _ = make_client(status, {})
    with pytest.raises(HomeAssistantError) as error:
        await client.get_request("path")
    assert not isinstance(error.value, TransientCloudError)


@pytest.mark.parametrize("status", [201, 307, 308])
async def test_real_http_device_post_never_follows_redirect(
    status, caplog, socket_enabled
):
    """A redirect cannot resend a selected device's command to another device."""
    calls = []
    selected_path = "/apiv1/dsns/selected-device/properties/set_status/datapoints.json"
    other_path = "/apiv1/dsns/other-device/properties/set_status/datapoints.json"

    async def handle(request):
        calls.append((request.path, await request.json()))
        if request.path == selected_path and status != 201:
            return web.Response(
                status=status,
                headers={"Location": other_path + "?token=secret-location"},
            )
        return web.json_response({"ok": True}, status=201)

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    caplog.set_level(logging.DEBUG)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:

        class LocalSession:
            """Route the API URL to the real local test server."""

            def post(self, url, **kwargs):
                return session.post(server.make_url(urlsplit(url).path), **kwargs)

        client = APIClient(LocalSession(), "en", "user@example.com", "password-secret")
        client.get_access_token = AsyncMock(return_value="access-token-secret")
        client.device_dsn = "selected-device"
        if status == 201:
            assert await client.set_status(Status.ON) == {"ok": True}
        else:
            with pytest.raises(HomeAssistantError, match=f"HTTP {status}") as error:
                await client.set_status(Status.ON)
            assert "secret-location" not in str(error.value)
            assert "other-device" not in str(error.value)
        assert calls == [(selected_path, {"datapoint": {"value": 1}})]
        assert "secret-location" not in caplog.text
        assert "access-token-secret" not in caplog.text


async def test_opt_in_redirects_are_rejected_before_any_device_post():
    """The shared transport cannot be overridden to follow a write redirect."""
    client, session, _ = make_client(201, {})
    with pytest.raises(ValueError, match="redirects"):
        await client.post_request("path", {}, allow_redirects=True)
    assert session.calls == []


@pytest.mark.parametrize("status", [200, 307, 308])
async def test_real_http_device_get_never_follows_redirect(
    status, caplog, socket_enabled
):
    """A redirect cannot read another device's properties."""
    calls = []
    selected_path = "/apiv1/dsns/selected-device/properties.json"
    other_path = "/apiv1/dsns/other-device/properties.json"

    async def handle(request):
        calls.append(request.path)
        if request.path == selected_path and status != 200:
            return web.Response(
                status=status,
                headers={"Location": other_path + "?token=secret-location"},
            )
        return web.json_response(
            [{"property": {"name": "current_humidity", "value": 50}}]
        )

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    caplog.set_level(logging.DEBUG)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:

        class LocalSession:
            """Route the API URL to the real local test server."""

            def get(self, url, **kwargs):
                return session.get(server.make_url(urlsplit(url).path), **kwargs)

        client = APIClient(LocalSession(), "en", "user@example.com", "password-secret")
        client.get_access_token = AsyncMock(return_value="access-token-secret")
        client.device_dsn = "selected-device"
        try:
            if status == 200:
                assert await client.get_current_humidity() == 50
            else:
                with pytest.raises(HomeAssistantError, match=f"HTTP {status}") as error:
                    await client.get_current_humidity()
                assert "secret-location" not in str(error.value)
                assert "other-device" not in str(error.value)
            assert calls == [selected_path]
            assert "secret-location" not in caplog.text
            assert "access-token-secret" not in caplog.text
        finally:
            await client.async_shutdown()


class AuthenticationResponse:
    """An authentication endpoint response whose body access is observable."""

    def __init__(self, status, *, body="", location=None):
        self.status = status
        self.body = body
        self.headers = {"Location": location} if location else {}
        self.released = False
        self.text_calls = 0

    async def text(self):
        self.text_calls += 1
        return self.body

    def release(self):
        self.released = True


def authentication_session(failing_step=None, status=503):
    """Model the eight authentication endpoints, including required redirects."""
    responses = [
        AuthenticationResponse(302, location="https://example.invalid/?context=ctx"),
        AuthenticationResponse(
            200, body=json.dumps({"ucid": "u", "gmid": "g", "gmidTicket": "t"})
        ),
        AuthenticationResponse(
            200, body=json.dumps({"sessionInfo": {"login_token": "token"}})
        ),
        AuthenticationResponse(
            200,
            body=json.dumps(
                {"UID": "uid", "UIDSignature": "sig", "signatureTimestamp": "1"}
            ),
        ),
        AuthenticationResponse(200, body="const consentObj2Sig = 'consent';"),
        AuthenticationResponse(302, location="https://example.invalid/?code=code"),
        AuthenticationResponse(200, body=json.dumps({"access_token": "idp"})),
        AuthenticationResponse(
            200,
            body=json.dumps(
                {"access_token": "access", "refresh_token": "refresh", "expires_in": 60}
            ),
        ),
    ]
    if failing_step is not None:
        responses[failing_step] = AuthenticationResponse(
            status,
            body="<html>provider-secret</html>",
            location="https://secret-location.invalid",
        )
    return (
        SimpleNamespace(
            get=AsyncMock(side_effect=[responses[i] for i in (0, 1, 4, 5)]),
            post=AsyncMock(side_effect=[responses[i] for i in (2, 3, 6, 7)]),
        ),
        responses,
    )


@pytest.mark.parametrize("step", range(8))
@pytest.mark.parametrize("status", [429, 503])
async def test_authentication_stops_on_transient_response(step, status, caplog):
    """Every auth endpoint checks retryable errors before parsing response data."""
    session, responses = authentication_session(step, status)
    client = APIClient(session, "en", "user@example.com", "password-secret")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TransientCloudError, match=f"HTTP {status}") as error:
        await client.authenticate()
    assert session.get.await_count + session.post.await_count == step + 1
    assert responses[step].text_calls == 0
    assert responses[step].released
    assert "provider-secret" not in str(error.value)
    assert "secret-location" not in str(error.value)
    assert "password-secret" not in caplog.text
    assert "provider-secret" not in caplog.text
    assert not client.invalid_credentials


async def test_authentication_recovers_after_transient_response():
    """A later attempt can authenticate, preserving required OAuth redirects."""
    session, _ = authentication_session(2)
    client = APIClient(session, "en", "user@example.com", "password")
    with pytest.raises(TransientCloudError):
        await client.authenticate()
    session, _ = authentication_session()
    client.session = session
    assert await client.authenticate()
    assert session.get.await_args_list[0].kwargs["allow_redirects"] is False
    assert session.get.await_args_list[3].kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [429, 503])
async def test_refresh_retries_without_falling_back_to_login(status, caplog):
    """Temporary refresh failures retain the token and can recover later."""
    client, session, response = make_client(status, {"error": "provider-secret"})
    client.refresh_token = "refresh-token-secret"
    client.get_new_refresh_token = AsyncMock(return_value="unexpected-login")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TransientCloudError, match=f"HTTP {status}") as error:
        await client.get_new_access_token()
    assert response.json_calls == 0
    assert response.released
    assert session.request.closed
    client.get_new_refresh_token.assert_not_awaited()
    assert client.refresh_token == "refresh-token-secret"
    assert "provider-secret" not in str(error.value)
    assert "refresh-token-secret" not in caplog.text
    client.session = FakeSession(
        FakeResponse(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 60,
            },
        )
    )
    assert await client.get_new_access_token() == "new-access"
    assert client.refresh_token == "new-refresh"
    client.get_new_refresh_token.assert_not_awaited()


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        ([], "DeLonghi Dehumidifier"),
        ([{}], "DeLonghi Dehumidifier"),
        ([{"product_name": None}], "DeLonghi Dehumidifier"),
        ([{"product_name": "  "}], "DeLonghi Dehumidifier"),
        ([{"product_name": 42}], "DeLonghi Dehumidifier"),
        ([{"product_name": "Living Room"}], "Living Room"),
    ],
)
async def test_product_name_falls_back_when_unavailable(properties, expected):
    """A missing optional cloud product name must not prevent entry creation."""
    client, _, _ = make_client(200, {})
    client.get_properties = AsyncMock(return_value=properties)
    assert await client.get_product_name() == expected


async def test_product_name_does_not_hide_cloud_errors():
    """A failed property request remains eligible for the setup retry path."""
    client, _, _ = make_client(200, {})
    client.get_properties = AsyncMock(side_effect=TransientCloudError("unavailable"))
    with pytest.raises(TransientCloudError, match="unavailable"):
        await client.get_product_name()
