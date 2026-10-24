"""The DeLonghi Dehumidifier integration."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry, OperationNotAllowed
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import entity_registry as er

from .client import (
    APIClient,
    DeviceNotAvailableError,
    InvalidCredentialsError,
    InvalidDeviceListError,
    NoDevicesError,
    TransientCloudError,
    usable_dsn,
)
from .const import DOMAIN, RUNTIME_REAUTH_ID
from .identity import (
    CONF_DSN,
    CONF_ENROLLMENT,
    CONF_TRANSFER,
    ClaimJournalError,
    active_commands,
    binding_lock,
    claimants,
    clear_binding_issue,
    client_settings_match,
    command_guard,
    create_binding_issue,
    device_needs_restart,
    device_unique_id,
    discard_enrollment,
    has_enrollment,
    reload_journal,
    require_restart,
    restart_required_entries,
    runtime_is_current,
    save_election,
    unloading_entries,
)

_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.HUMIDIFIER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DeLonghiDehumidifierConfigEntry = ConfigEntry[APIClient]

_LOGGER = logging.getLogger(__name__)


def _check_setup_current(hass, entry, dsn, client):
    """Reject settings changes separately from actual device binding failures."""
    if device_needs_restart(hass, dsn):
        raise HomeAssistantError("Restart Home Assistant before using this integration")
    if (
        hass.config_entries.async_get_entry(entry.entry_id) is not entry
        or entry.disabled_by
        or not client_settings_match(entry, client)
    ):
        raise ConfigEntryNotReady("Integration settings changed during setup") from None
    if (
        entry.data.get(CONF_DSN) != dsn
        or entry.unique_id != device_unique_id(dsn)
        or any(owner is not entry for owner in claimants(hass, dsn))
    ):
        create_binding_issue(hass, entry)
        raise ConfigEntryNotReady("Device identity needs recovery")


async def _cleanup_platforms(hass, entry, dsn):
    """Avoid reusing an entry if cleanup may have stopped partway through."""
    try:
        result = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    except asyncio.CancelledError:
        require_restart(hass, entry.entry_id, dsn)
        raise
    except Exception:  # noqa: BLE001 - any partial cleanup requires a restart
        require_restart(hass, entry.entry_id, dsn)
        raise HomeAssistantError(
            "Restart Home Assistant before reloading this integration"
        ) from None
    if not result:
        require_restart(hass, entry.entry_id, dsn)
        raise HomeAssistantError(
            "Restart Home Assistant before reloading this integration"
        )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade the schema offline; device binding happens during setup."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(entry, version=2, minor_version=1)
    return entry.version == 2


async def async_setup_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Set up DeLonghi Dehumidifier from a config entry."""

    if entry.entry_id in restart_required_entries(hass):
        raise HomeAssistantError(
            "Restart Home Assistant before reloading this integration"
        )
    if (
        entry.disabled_by
        or hass.config_entries.async_get_entry(entry.entry_id) is not entry
    ):
        raise ConfigEntryNotReady("Integration is not enabled")
    if device_needs_restart(hass, entry.data.get(CONF_DSN)):
        raise HomeAssistantError("Restart Home Assistant before using this integration")
    async with binding_lock(hass):
        try:
            journal = await reload_journal(hass)
        except ClaimJournalError as err:
            if device_needs_restart(hass, entry.data.get(CONF_DSN)):
                raise HomeAssistantError(
                    "Restart Home Assistant before using this integration"
                ) from None
            if (
                entry.disabled_by
                or hass.config_entries.async_get_entry(entry.entry_id) is not entry
            ):
                raise ConfigEntryNotReady("Integration is not enabled") from None
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Device ownership needs recovery") from err
        if (
            entry.disabled_by
            or hass.config_entries.async_get_entry(entry.entry_id) is not entry
        ):
            raise ConfigEntryNotReady("Integration is not enabled")
        dsn = entry.data.get(CONF_DSN)
        if device_needs_restart(hass, dsn):
            raise HomeAssistantError(
                "Restart Home Assistant before using this integration"
            )
        if dsn is None:
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Device identity needs confirmation")
        elif (
            not usable_dsn(dsn)
            or entry.unique_id != device_unique_id(dsn)
            or any(owner is not entry for owner in claimants(hass, dsn))
        ):
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Device identity needs recovery")
        election = journal.get(device_unique_id(dsn))
        transfer = bool(entry.data.get(CONF_TRANSFER))
        token = entry.data.get(CONF_ENROLLMENT)
        enrolling = has_enrollment(hass, token, dsn)
        if (
            election is None
            and not enrolling
            or election is not None
            and election["owner"] != entry.entry_id
            and not (
                enrolling
                and transfer
                and hass.config_entries.async_get_entry(election["owner"]) is None
                and not active_commands(hass, election["owner"])
            )
        ):
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Device identity needs recovery")

    settings = {**entry.data, **entry.options}
    language = settings[CONF_LANGUAGE]
    email = settings[CONF_EMAIL]
    password = settings[CONF_PASSWORD]

    session = aiohttp_client.async_get_clientsession(hass)
    client = APIClient(session, language, email, password)
    setup_complete = False
    platforms_may_be_loaded = False
    try:
        try:
            if not await client.authenticate():
                raise ConfigEntryNotReady("Unable to authenticate with DeLonghi")
            await client.verify_device(dsn)
            async with binding_lock(hass):
                current_journal = await reload_journal(hass)
                _check_setup_current(hass, entry, dsn, client)
                if (
                    bool(entry.data.get(CONF_TRANSFER)) != transfer
                    or entry.data.get(CONF_ENROLLMENT) != token
                ):
                    raise ConfigEntryNotReady(
                        "Integration settings changed during setup"
                    )
                if current_journal.get(device_unique_id(dsn)) != election:
                    create_binding_issue(hass, entry)
                    raise ConfigEntryNotReady("Device identity needs recovery")
                if election is None or election["owner"] != entry.entry_id:
                    if not enrolling or (
                        election and active_commands(hass, election["owner"])
                    ):
                        create_binding_issue(hass, entry)
                        raise ConfigEntryNotReady("Device ownership needs recovery")
                    await save_election(hass, dsn, entry.entry_id, set())
                    _check_setup_current(hass, entry, dsn, client)
                    if (
                        bool(entry.data.get(CONF_TRANSFER)) != transfer
                        or entry.data.get(CONF_ENROLLMENT) != token
                    ):
                        raise ConfigEntryNotReady(
                            "Integration settings changed during setup"
                        )
                if transfer or token:
                    data = dict(entry.data)
                    data.pop(CONF_TRANSFER, None)
                    data.pop(CONF_ENROLLMENT, None)
                    hass.config_entries.async_update_entry(entry, data=data)
                    discard_enrollment(hass, token)
                clear_binding_issue(hass, entry)
            # Platforms need these properties to construct device information. Read
            # them while setup can still request a retry, before adding any entities.
            # Their subsequent reads share the client's normal property cache.
            await client.get_properties()
            _check_setup_current(hass, entry, dsn, client)
        except InvalidCredentialsError as err:
            _check_setup_current(hass, entry, dsn, client)
            raise ConfigEntryAuthFailed from err
        except DeviceNotAvailableError as err:
            _check_setup_current(hass, entry, dsn, client)
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Selected device is not available") from err
        except ClaimJournalError as err:
            _check_setup_current(hass, entry, dsn, client)
            create_binding_issue(hass, entry)
            raise ConfigEntryNotReady("Device ownership needs recovery") from err
        except NoDevicesError as err:
            raise ConfigEntryNotReady(
                "No devices found in the DeLonghi account"
            ) from err
        except (aiohttp.ClientError, TimeoutError, TransientCloudError) as err:
            raise ConfigEntryNotReady("Unable to contact DeLonghi") from err
        except InvalidDeviceListError as err:
            raise ConfigEntryNotReady("Unable to discover DeLonghi devices") from err

        entry.runtime_data = client
        client.device_write_guard = lambda: command_guard(hass, entry, dsn, client)

        def start_reauth() -> None:
            """Request reauthentication only for the active runtime client."""
            if runtime_is_current(hass, entry, client):
                entry.async_start_reauth(
                    hass, data={RUNTIME_REAUTH_ID: client.reauth_id}
                )

        client.on_invalid_credentials = start_reauth
        platforms_may_be_loaded = True
        try:
            await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
        except asyncio.CancelledError:
            require_restart(hass, entry.entry_id, dsn)
            raise
        except Exception:  # noqa: BLE001 - forwarding may have partially completed
            require_restart(hass, entry.entry_id, dsn)
            raise HomeAssistantError(
                "Restart Home Assistant before reloading this integration"
            ) from None
        setup_error = None
        try:
            _check_setup_current(hass, entry, dsn, client)
        except ConfigEntryNotReady as err:
            setup_error = err
        if setup_error is not None or client.invalid_credentials:
            await _cleanup_platforms(hass, entry, dsn)
            platforms_may_be_loaded = False
            # New settings may arrive while the platforms are being removed.
            _check_setup_current(hass, entry, dsn, client)
            if setup_error is not None:
                raise setup_error
            raise ConfigEntryAuthFailed
        _remove_retired_buttons(hass, entry)
        entry.async_on_unload(entry.add_update_listener(_async_update_options))
        setup_complete = True
        return True
    finally:
        if not setup_complete:
            if platforms_may_be_loaded:
                require_restart(hass, entry.entry_id, dsn)
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
    if entry.entry_id in restart_required_entries(hass):
        _LOGGER.warning("Restart Home Assistant before reloading this integration")
        return
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except OperationNotAllowed:
        if entry.entry_id not in restart_required_entries(hass):
            raise
        _LOGGER.warning("Restart Home Assistant before reloading this integration")


async def async_unload_entry(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> bool:
    """Unload a config entry."""
    if entry.entry_id in restart_required_entries(hass):
        raise HomeAssistantError(
            "Restart Home Assistant before reloading this integration"
        )
    unloading_entries(hass).add(entry.entry_id)
    dsn = entry.runtime_data.device_dsn
    completed = False
    try:
        await _cleanup_platforms(hass, entry, dsn)
        await entry.runtime_data.async_shutdown()
        completed = True
        return True
    finally:
        if completed:
            unloading_entries(hass).discard(entry.entry_id)
        else:
            require_restart(hass, entry.entry_id, dsn)
            await entry.runtime_data.async_shutdown()
