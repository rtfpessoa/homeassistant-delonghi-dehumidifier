"""Tests for DeLonghi API HTTP responses."""

import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.delonghi_dehumidifier_api.client import APIClient, OffOnStatus
from custom_components.delonghi_dehumidifier_api.switch import GenericOffOnSwitchSensor


class FakeResponse:
    """An HTTP response containing JSON."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.json_calls = 0

    async def json(self):
        self.json_calls += 1
        return self.body


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
