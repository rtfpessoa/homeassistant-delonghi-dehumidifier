"""Test provider login errors through Home Assistant configuration flows."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

CONFIG = {
    CONF_LANGUAGE: "en",
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "not-a-real-password",
}


def login_error_session(error_code):
    """Return a synthetic session reaching the real provider login code."""
    return SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status=302,
                    headers={"Location": "https://example.invalid/?context=test"},
                ),
                SimpleNamespace(
                    status=200,
                    text=AsyncMock(
                        return_value=json.dumps(
                            {"ucid": "ucid", "gmid": "gmid", "gmidTicket": "ticket"}
                        )
                    ),
                ),
            ]
        ),
        post=AsyncMock(
            return_value=SimpleNamespace(
                status=200,
                text=AsyncMock(
                    return_value=json.dumps(
                        {
                            "errorCode": error_code,
                            "statusCode": 500 if error_code >= 500000 else 403,
                        }
                    )
                ),
            )
        ),
    )


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [(403042, "invalid_auth"), (403120, "unknown"), (500001, "unknown")],
)
async def test_user_flow_classifies_login_errors(hass, error_code, expected):
    """Wrong credentials are distinct from lockout and provider errors."""
    session = login_error_session(error_code)
    with patch(
        "custom_components.delonghi_dehumidifier_api.config_flow.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=CONFIG
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == expected
    assert session.post.await_count == 1


async def test_options_flow_reports_invalid_credentials(hass):
    """Existing entries get the same feedback without saving invalid settings."""
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG)
    entry.add_to_hass(hass)
    session = login_error_session(403042)
    with (
        patch(
            "custom_components.delonghi_dehumidifier_api.async_setup_entry",
            return_value=True,
        ),
        patch(
            "custom_components.delonghi_dehumidifier_api.config_flow.aiohttp_client.async_get_clientsession",
            return_value=session,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=CONFIG
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"
    assert entry.options == {}
    assert session.post.await_count == 1


async def test_successful_authentication_passes_login_token_to_provider():
    """A successful provider login continues through the full token exchange."""

    def json_response(data):
        return SimpleNamespace(
            status=200, text=AsyncMock(return_value=json.dumps(data))
        )

    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status=302,
                    headers={
                        "Location": "https://example.invalid/?context=test-context"
                    },
                ),
                json_response(
                    {"ucid": "test-ucid", "gmid": "test-gmid", "gmidTicket": "ticket"}
                ),
                SimpleNamespace(
                    status=200,
                    text=AsyncMock(return_value="const consentObj2Sig = 'signature';"),
                ),
                SimpleNamespace(
                    status=302,
                    headers={"Location": "https://example.invalid/?code=test-code"},
                ),
            ]
        ),
        post=AsyncMock(
            side_effect=[
                json_response(
                    {"errorCode": 0, "sessionInfo": {"login_token": "login-token"}}
                ),
                json_response(
                    {
                        "UID": "uid",
                        "UIDSignature": "uid-signature",
                        "signatureTimestamp": "123",
                    }
                ),
                json_response({"access_token": "idp-token"}),
                json_response(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    }
                ),
            ]
        ),
    )
    client = APIClient(session, "en", "test@example.com", "not-a-real-password")

    assert await client.authenticate() is True
    assert client.access_token == "access-token"
    assert client.refresh_token == "refresh-token"
    user_info = session.post.await_args_list[1]
    assert user_info.args[0] == "https://socialize.eu1.gigya.com/socialize.getUserInfo"
    assert user_info.kwargs["data"]["login_token"] == "login-token"
    assert (
        session.get.await_args_list[3].kwargs["params"]["login_token"] == "login-token"
    )
    assert session.post.await_args_list[3].kwargs["data"]["token"] == "idp-token"
