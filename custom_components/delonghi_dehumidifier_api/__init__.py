"""The DeLonghi Dehumidifier integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .client import APIClient

_PLATFORMS: list[Platform] = [
    Platform.HUMIDIFIER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DeLonghiDehumidifierConfigEntry = ConfigEntry[APIClient]


async def async_setup_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Set up DeLonghi Dehumidifier from a config entry."""

    language = entry.data[CONF_LANGUAGE]
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    session = aiohttp_client.async_get_clientsession(hass)
    entry.runtime_data = APIClient(session, language, email, password)

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
