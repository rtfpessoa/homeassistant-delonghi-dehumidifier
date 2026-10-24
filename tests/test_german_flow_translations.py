"""Test German config flow translations through Home Assistant."""

import json
import string
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import translation
from homeassistant.helpers.translation import async_get_translations, recursive_flatten
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import InvalidCredentialsError
from custom_components.delonghi_dehumidifier_api.config_flow import InvalidAuth
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

CONFIG = {
    CONF_LANGUAGE: "pt",
    CONF_EMAIL: "account@example.com",
    CONF_PASSWORD: "not-a-real-password",
}
PREFIX = f"component.{DOMAIN}"
TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / DOMAIN / "translations"


@pytest.mark.parametrize("category", ["config", "options"])
async def test_german_is_complete_and_loaded(hass, category):
    """The runtime loader resolves every key with matching placeholders."""
    source = json.loads((TRANSLATIONS / "de.json").read_text(encoding="utf-8"))
    expected = recursive_flatten(f"{PREFIX}.{category}.", source[category])
    german = await async_get_translations(hass, "de", category, [DOMAIN])
    english = await async_get_translations(hass, "en", category, [DOMAIN])
    assert german == expected
    assert german.keys() == english.keys()
    for key, value in german.items():
        assert value and "[%key:" not in value
        german_fields = {
            name for _, name, _, _ in string.Formatter().parse(value) if name
        }
        english_fields = {
            name for _, name, _, _ in string.Formatter().parse(english[key]) if name
        }
        assert german_fields == english_fields


@pytest.mark.parametrize("flow_type", ["config", "options"])
async def test_german_flow_error_keeps_account_language(hass, flow_type):
    """The interface language is separate from the account language in the form."""
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

    german = await async_get_translations(hass, "de", flow_type, [DOMAIN])
    assert german[f"{PREFIX}.{flow_type}.step.{result['step_id']}.data.email"] == (
        "E-Mail-Adresse"
    )
    with patch(
        "custom_components.delonghi_dehumidifier_api.config_flow.validate_input",
        AsyncMock(side_effect=InvalidAuth()),
    ) as validate:
        result = await manager.async_configure(result["flow_id"], user_input=CONFIG)
    assert validate.await_args.args[1][CONF_LANGUAGE] == "pt"
    assert result["errors"]["base"] == "invalid_auth"
    assert german[f"{PREFIX}.{flow_type}.error.invalid_auth"] == (
        "Die Anmeldung ist fehlgeschlagen. Prüfe deine E-Mail-Adresse und dein "
        "Passwort oder versuche es später erneut."
    )


async def test_german_reauth_uses_effective_email_and_account_language(hass):
    """Interpolate the effective account email without changing account settings."""
    options = {CONF_LANGUAGE: "fr", CONF_EMAIL: "effective@example.com"}
    entry = MockConfigEntry(domain=DOMAIN, data=CONFIG, options=options)
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=CONFIG,
    )
    german = await async_get_translations(hass, "de", "config", [DOMAIN])
    description = german[f"{PREFIX}.config.step.reauth_confirm.description"]
    assert description.format(**result["description_placeholders"]) == (
        "Gib das Passwort für effective@example.com ein, um dein De'Longhi-Konto "
        "erneut zu verbinden."
    )
    with patch(
        "custom_components.delonghi_dehumidifier_api.config_flow.APIClient"
    ) as client:
        client.return_value.async_shutdown = AsyncMock()
        client.return_value.authenticate = AsyncMock(
            side_effect=InvalidCredentialsError("invalid credentials")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )
    assert result["errors"]["base"] == "invalid_auth"
    assert german[f"{PREFIX}.config.error.{result['errors']['base']}"]
    assert client.call_args.args[1:] == ("fr", "effective@example.com", "new-password")
    assert entry.data == CONFIG
    assert entry.options == options


async def test_missing_german_key_falls_back_to_english(hass):
    """A partial German variant keeps its keys and falls back for a missing one."""
    load_json = translation.load_json

    def load_partial(path):
        if path.name == "de-AT.json":
            data = load_json(path.with_name("de.json"))
            del data["options"]["error"]["unknown"]
            return data
        return load_json(path)

    with patch.object(translation, "load_json", side_effect=load_partial):
        german = await async_get_translations(hass, "de-AT", "options", [DOMAIN])
        english = await async_get_translations(hass, "en", "options", [DOMAIN])
    assert (
        german[f"{PREFIX}.options.error.unknown"]
        == english[f"{PREFIX}.options.error.unknown"]
    )
    assert german[f"{PREFIX}.options.step.init.data.email"] == "E-Mail-Adresse"
