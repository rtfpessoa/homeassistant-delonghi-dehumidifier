"""The DeLonghi Dehumidifier integration."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import entity_registry as er

from .client import (
    APIClient,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
)
from .const import DOMAIN, RUNTIME_REAUTH_ID

_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
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
    client = APIClient(session, language, email, password)
    setup_complete = False
    try:
        try:
            authenticated = await client.authenticate()
            if not authenticated:
                raise ConfigEntryNotReady("Unable to authenticate with DeLonghi")
            await client.get_first_device()
            # Platforms need these properties to construct device information. Read
            # them while setup can still request a retry, before adding any entities.
            # Their subsequent reads share the client's normal property cache.
            await client.get_properties()
        except InvalidCredentialsError as err:
            raise ConfigEntryAuthFailed from err
        except NoDevicesError as err:
            raise ConfigEntryNotReady(
                "No devices found in the DeLonghi account"
            ) from err
        except (aiohttp.ClientError, TimeoutError, TransientCloudError) as err:
            raise ConfigEntryNotReady("Unable to contact DeLonghi") from err

        entry.runtime_data = client

        def start_reauth() -> None:
            """Request reauthentication only for the active runtime client."""
            if entry.state is ConfigEntryState.LOADED and entry.runtime_data is client:
                entry.async_start_reauth(
                    hass, data={RUNTIME_REAUTH_ID: client.reauth_id}
                )

        client.on_invalid_credentials = start_reauth
        await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
        if client.invalid_credentials:
            # Authentication can also fail while platforms perform their first reads.
            await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
            raise ConfigEntryAuthFailed
        _remove_retired_buttons(hass, entry)
        entry.async_on_unload(entry.add_update_listener(_async_update_options))
        setup_complete = True
        return True
    finally:
        if not setup_complete:
            await client.async_shutdown()


def _remove_retired_buttons(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove old Real Feel buttons, including disabled or renamed entries."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.domain == Platform.BUTTON
            and entity.platform == DOMAIN
            and entity.unique_id.startswith(f"{DOMAIN}_")
            and entity.unique_id.endswith("_request_exit_real_feel")
        ):
            registry.async_remove(entity.entity_id)


async def _async_update_options(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> None:
    """Reload the entry after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    await entry.runtime_data.async_shutdown()
    return True
