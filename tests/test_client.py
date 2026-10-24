"""Tests for the DeLonghi API client."""

import logging
import time
import traceback
from unittest.mock import AsyncMock

import pytest

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidNumericStateError,
)
from custom_components.delonghi_dehumidifier_api.humidifier import _int_property


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (0, 0),
        (-17, -17),
        (42.0, 42),
        (-0.0, 0),
        (" +0012 ", 12),
        ("-12", -12),
        ("١٢", 12),
        ("1_000", 1000),
    ],
)
async def test_integer_cloud_state_accepts_supported_values(raw, expected):
    """Both readers preserve the existing supported integral representations."""
    client = APIClient(None, "en", "user", "password")
    client.get_property = AsyncMock(return_value=raw)
    assert await client.get_int_property("value") == expected
    assert _int_property([{"name": "value", "value": raw}], "value") == expected
    assert _int_property([], "value") is None


@pytest.mark.parametrize(
    "raw",
    [
        True,
        False,
        1.5,
        -1.5,
        float("nan"),
        float("inf"),
        -float("inf"),
        "1.0",
        "secret-invalid-number",
        "9" * 5000,
        "１２.０",
        [],
        {},
        object(),
    ],
)
async def test_invalid_integer_cloud_state_is_sanitized(raw):
    """Malformed states cannot be coerced or exposed by conversion exceptions."""
    client = APIClient(None, "en", "user", "password")
    client.get_property = AsyncMock(return_value=raw)
    for read in (
        lambda: client.get_int_property("value"),
        lambda: _read_snapshot_integer(raw),
    ):
        with pytest.raises(InvalidNumericStateError) as error:
            await read()
        rendered = "".join(traceback.format_exception(error.value))
        assert str(error.value) == "Invalid numeric cloud state"
        assert "secret-invalid-number" not in rendered
        assert "9" * 5000 not in rendered


async def _read_snapshot_integer(raw):
    return _int_property([{"name": "value", "value": raw}], "value")


class FakeResponse:
    """An aiohttp-like response with a JSON body."""

    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def json(self):
        return self.body


class FakeRequest:
    """An aiohttp request can be awaited or used as an async context manager."""

    def __init__(self, response):
        self.response = response
        self.exited = False

    def __await__(self):
        async def get_response():
            return self.response

        return get_response().__await__()

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        self.exited = True


class FakeSession:
    """A session that records one refresh request."""

    def __init__(self, response):
        self.request = FakeRequest(response)
        self.post = self._post
        self.calls = []

    def _post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.request


@pytest.mark.asyncio
async def test_refreshes_expired_access_token_without_logging_secrets(caplog):
    """A valid JSON refresh updates the tokens and does not log their values."""
    old_refresh = "old-refresh-secret"
    new_refresh = "new-refresh-secret"
    new_access = "new-access-secret"
    session = FakeSession(
        FakeResponse(
            200,
            {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_in": 3600,
            },
        )
    )
    client = APIClient(session, "en", "user@example.com", "password")
    client.access_token = "expired-access-secret"
    client.refresh_token = old_refresh
    client.token_expiry = 0
    caplog.set_level(logging.DEBUG, logger=client.__module__)

    before = time.time()
    assert await client.get_access_token() == new_access
    after = time.time()

    assert client.access_token == new_access
    assert client.refresh_token == new_refresh
    assert before + 3600 <= client.token_expiry <= after + 3600
    assert len(session.calls) == 1
    assert session.calls[0][1]["json"] == {"user": {"refresh_token": old_refresh}}
    assert session.request.exited
    for secret in (old_refresh, new_refresh, new_access, "expired-access-secret"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_failed_refresh_falls_back_to_login_without_logging_response(caplog):
    """An unsuccessful refresh releases its response before retrying login."""
    error_secret = "error-response-secret"
    old_refresh = "old-refresh-secret"
    session = FakeSession(FakeResponse(401, {"error": error_secret}))
    client = APIClient(session, "en", "user@example.com", "password")
    client.refresh_token = old_refresh
    caplog.set_level(logging.DEBUG, logger=client.__module__)

    async def login():
        assert session.request.exited
        return "login-access-token"

    client.get_new_refresh_token = AsyncMock(side_effect=login)

    assert await client.get_access_token() == "login-access-token"
    client.get_new_refresh_token.assert_awaited_once()
    assert error_secret not in caplog.text
    assert old_refresh not in caplog.text
