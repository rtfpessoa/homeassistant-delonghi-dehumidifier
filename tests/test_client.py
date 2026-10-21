"""Tests for the DeLonghi API client."""

import logging
import time
from unittest.mock import AsyncMock

import pytest

from custom_components.delonghi_dehumidifier_api.client import APIClient


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
