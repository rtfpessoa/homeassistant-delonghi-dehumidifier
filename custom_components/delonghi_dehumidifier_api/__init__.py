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

    settings = {**entry.data, **entry.options}
    language = settings[CONF_LANGUAGE]
    email = settings[CONF_EMAIL]
    password = settings[CONF_PASSWORD]

    session = aiohttp_client.async_get_clientsession(hass)
    entry.runtime_data = APIClient(session, language, email, password)

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> None:
    """Reload the entry after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
