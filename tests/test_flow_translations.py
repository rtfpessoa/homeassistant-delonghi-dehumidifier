"""Exercise custom integration translations through Home Assistant flows."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.config_flow import InvalidAuth
from custom_components.delonghi_dehumidifier_api.const import DOMAIN, RUNTIME_REAUTH_ID

CONFIG = {
    CONF_LANGUAGE: "pt",
    CONF_EMAIL: "account@example.com",
    CONF_PASSWORD: "not-a-real-password",
}
PREFIX = f"component.{DOMAIN}"
INTEGRATION_PATH = Path(__file__).parents[1] / "custom_components" / DOMAIN


def test_runtime_english_matches_source():
    """Keep the shipped custom-integration translations in sync with the source."""
    source = json.loads((INTEGRATION_PATH / "strings.json").read_text())
    runtime = json.loads((INTEGRATION_PATH / "translations" / "en.json").read_text())
    assert source == runtime


@pytest.mark.parametrize("flow_type", ["config", "options"])
@pytest.mark.parametrize(
    ("failure", "expected"),
    [(InvalidAuth(), "invalid_auth"), (RuntimeError("failure"), "unknown")],
)
async def test_setup_and_options_labels_and_errors(hass, flow_type, failure, expected):
    """Each form field and returned error has a usable runtime translation."""
    if flow_type == "config":
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        manager = hass.config_entries.flow
    else:
        entry = MockConfigEntry(domain=DOMAIN, data=CONFIG)
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        manager = hass.config_entries.options

    translations = await async_get_translations(hass, "en", flow_type, [DOMAIN])
    step = result["step_id"]
    assert {str(field) for field in result["data_schema"].schema} == {
        CONF_LANGUAGE,
        CONF_EMAIL,
        CONF_PASSWORD,
    }
    for field in (CONF_LANGUAGE, CONF_EMAIL, CONF_PASSWORD):
        text = translations[f"{PREFIX}.{flow_type}.step.{step}.data.{field}"]
        assert text and "[%key:" not in text

    with patch(
        "custom_components.delonghi_dehumidifier_api.config_flow.validate_input",
        AsyncMock(side_effect=failure),
    ):
        result = await manager.async_configure(result["flow_id"], user_input=CONFIG)

    assert result["errors"]["base"] == expected
    text = translations[f"{PREFIX}.{flow_type}.error.{expected}"]
    assert text and "[%key:" not in text


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (InvalidCredentialsError("invalid credentials"), "invalid_auth"),
        (aiohttp.ClientConnectionError("unavailable"), "cannot_connect"),
        (RuntimeError("failure"), "unknown"),
    ],
)
async def test_reauth_error_translations_and_account_language(hass, failure, expected):
    """French UI fallback does not alter the vendor-account language or email."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=CONFIG,
        options={CONF_LANGUAGE: "de", CONF_EMAIL: "effective@example.com"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=CONFIG,
    )
    assert result["step_id"] == "reauth_confirm"

    english = await async_get_translations(hass, "en", "config", [DOMAIN])
    fallback = await async_get_translations(hass, "fr", "config", [DOMAIN])
    assert fallback == english
    assert all(value and "[%key:" not in value for value in english.values())
    description = fallback[f"{PREFIX}.config.step.reauth_confirm.description"]
    assert description.format(**result["description_placeholders"]) == (
        "Enter the password for effective@example.com to reconnect your De'Longhi account."
    )
    assert fallback[f"{PREFIX}.config.step.reauth_confirm.data.password"]
    for reason in (
        "reauth_successful",
        "reauth_settings_changed",
        "reauth_cancelled",
    ):
        assert fallback[f"{PREFIX}.config.abort.{reason}"]

    with patch(
        "custom_components.delonghi_dehumidifier_api.config_flow.APIClient"
    ) as client:
        client.return_value.authenticate = AsyncMock(side_effect=failure)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )

    assert result["errors"]["base"] == expected
    assert fallback[f"{PREFIX}.config.error.{expected}"]
    assert client.call_args.args[1:] == ("de", "effective@example.com", "new-password")
    assert entry.data == CONFIG
    assert entry.options == {CONF_LANGUAGE: "de", CONF_EMAIL: "effective@example.com"}


async def test_cancelled_reauth_uses_translated_abort_reason(hass):
    """A stale reauthentication request has a user-readable abort reason."""
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data={**CONFIG, RUNTIME_REAUTH_ID: "stale"},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_cancelled"
    translations = await async_get_translations(hass, "en", "config", [DOMAIN])
    assert translations[f"{PREFIX}.config.abort.{result['reason']}"]


async def test_options_fallback_to_english(hass):
    """Languages without a translation use the complete English options text."""
    english = await async_get_translations(hass, "en", "options", [DOMAIN])
    fallback = await async_get_translations(hass, "fr", "options", [DOMAIN])
    assert english
    assert fallback == english
    assert all(value and "[%key:" not in value for value in english.values())
