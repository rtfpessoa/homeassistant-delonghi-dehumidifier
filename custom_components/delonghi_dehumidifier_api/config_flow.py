"""Config flow for the DeLonghi Dehumidifier integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from . import DeLonghiDehumidifierConfigEntry, _async_update_options
from .client import (
    APIClient,
    CloudPermissionDeniedError,
    DeviceNotAvailableError,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
    usable_dsn,
)
from .const import DOMAIN, RUNTIME_REAUTH_ID
from .identity import (
    CONF_DSN,
    CONF_ENROLLMENT,
    CONF_TRANSFER,
    ISSUE_PREFIX,
    ClaimJournalError,
    binding_lock,
    claimants,
    device_needs_restart,
    device_unique_id,
    pending_claim,
    read_device_owner,
    reload_journal,
    reserve_enrollment,
    restart_required_entries,
    runtime_is_current,
)

# Home Assistant 2026.9+ aliases voluptuous to Probatio at runtime.
if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LANGUAGE, default="en"): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_device_settings(
    hass: HomeAssistant, data: dict[str, Any], dsn: str
) -> tuple[str, str | None]:
    """Return the verified device title or a safe form error."""
    client = APIClient(
        aiohttp_client.async_get_clientsession(hass),
        data[CONF_LANGUAGE],
        data[CONF_EMAIL],
        data[CONF_PASSWORD],
    )
    try:
        if not await client.authenticate():
            return "", "invalid_auth"
        await client.verify_device(dsn)
        return await client.get_product_name(), None
    except InvalidCredentialsError:
        return "", "invalid_auth"
    except CloudPermissionDeniedError:
        return "", "permission_denied"
    except (aiohttp.ClientError, TimeoutError, TransientCloudError):
        return "", "cannot_connect"
    except NoDevicesError:
        return "", "no_devices"
    except DeviceNotAvailableError:
        return "", "device_not_available"
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Unexpected device validation failure (%s)", type(err).__name__)
        return "", "unknown"
    finally:
        await client.async_shutdown()


class DeLonghiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DeLonghi Dehumidifier."""

    VERSION = 2

    _account_edit: _AccountEdit | None = None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the account settings for this entry’s selected appliance."""
        if self._account_edit is None:
            self._account_edit = _AccountEdit(self._get_reconfigure_entry())
        return await self._account_edit.async_step(self, user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for the existing entry."""
        runtime_id = entry_data.get(RUNTIME_REAUTH_ID)
        if runtime_id is not None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if (
                entry is None
                or entry.state is not ConfigEntryState.LOADED
                or entry.runtime_data.reauth_id != runtime_id
                or not runtime_is_current(self.hass, entry, entry.runtime_data)
            ):
                return self.async_abort(reason="reauth_cancelled")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate a new password and reload the existing entry."""
        entry = self._get_reauth_entry()
        original_data, original_options = dict(entry.data), dict(entry.options)
        settings = {**original_data, **original_options}
        errors: dict[str, str] = {}
        if user_input is not None:
            session = aiohttp_client.async_get_clientsession(self.hass)
            client = APIClient(
                session,
                settings[CONF_LANGUAGE],
                settings[CONF_EMAIL],
                user_input[CONF_PASSWORD],
            )
            try:
                authenticated = await client.authenticate()
                if authenticated and (dsn := entry.data.get(CONF_DSN)):
                    await client.verify_device(dsn)
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except CloudPermissionDeniedError:
                errors["base"] = "permission_denied"
            except (aiohttp.ClientError, TimeoutError, TransientCloudError):
                errors["base"] = "cannot_connect"
            except (NoDevicesError, DeviceNotAvailableError):
                errors["base"] = "device_not_available"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "Unexpected reauthentication failure (%s)", type(err).__name__
                )
                errors["base"] = "unknown"
            else:
                if authenticated:
                    return self._finish_reauth(
                        entry,
                        original_data,
                        original_options,
                        user_input[CONF_PASSWORD],
                    )
                errors["base"] = "unknown"
            finally:
                await client.async_shutdown()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={CONF_EMAIL: settings[CONF_EMAIL]},
            errors=errors,
        )

    @callback
    def _finish_reauth(
        self,
        entry: DeLonghiDehumidifierConfigEntry,
        original_data: dict[str, Any],
        original_options: dict[str, Any],
        password: str,
    ) -> ConfigFlowResult:
        """Apply verified credentials if the entry and flow are still current."""
        if (
            self.hass.config_entries.async_get_entry(entry.entry_id) is not entry
            or entry.data != original_data
            or entry.options != original_options
        ):
            return self.async_abort(reason="reauth_settings_changed")
        if not any(
            flow["flow_id"] == self.flow_id
            for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        ):
            return self.async_abort(reason="reauth_cancelled")

        options = dict(entry.options)
        options.pop(CONF_PASSWORD, None)
        had_listener = bool(entry.update_listeners)
        changed = self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PASSWORD: password}, options=options
        )
        if not changed or not had_listener:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = APIClient(
                aiohttp_client.async_get_clientsession(self.hass),
                user_input[CONF_LANGUAGE],
                user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD],
            )
            try:
                if not await client.authenticate():
                    raise InvalidAuth
                devices = await client.get_devices()
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except CloudPermissionDeniedError:
                errors["base"] = "permission_denied"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, TransientCloudError):
                errors["base"] = "cannot_connect"
            except NoDevicesError:
                errors["base"] = "no_devices"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Unexpected setup failure (%s)", type(err).__name__)
                errors["base"] = "unknown"
            else:
                self._settings = dict(user_input)
                self._devices = devices
                return await self.async_step_select_device()
            finally:
                await client.async_shutdown()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to select a discovered identity explicitly."""
        if user_input is not None:
            return await self._select_device(user_input[CONF_DSN])
        return self.async_show_form(
            step_id="select_device",
            data_schema=self._selection_schema(),
        )

    def _selection_schema(self) -> vol.Schema:
        registry = dr.async_get(self.hass)
        labels = {dsn: dsn for dsn in self._devices}
        devices = registry.devices
        if isinstance(devices, Mapping):
            devices = devices.values()
        for device in devices:
            name = device.name_by_user or device.name
            if name:
                for domain, dsn in device.identifiers:
                    if domain == DOMAIN and dsn in labels:
                        labels[dsn] = f"{name} ({dsn})"
        return vol.Schema({vol.Required(CONF_DSN): vol.In(labels)})

    async def _select_device(self, dsn: str) -> ConfigFlowResult:
        """Verify a selection again, then reserve its identity before creating an entry."""
        title, error = await _validate_device_settings(self.hass, self._settings, dsn)
        if error is None:
            return await self._enroll_selected_device(dsn, title)
        return self.async_show_form(
            step_id="select_device",
            data_schema=self._selection_schema(),
            errors={"base": error},
        )

    async def _enroll_selected_device(self, dsn: str, title: str) -> ConfigFlowResult:
        """Reserve and create the selected entry in one locked transaction."""
        async with binding_lock(self.hass):
            if device_needs_restart(self.hass, dsn):
                return self.async_abort(reason="restart_required")
            try:
                journal = await reload_journal(self.hass)
            except ClaimJournalError:
                if device_needs_restart(self.hass, dsn):
                    return self.async_abort(reason="restart_required")
                return self.async_show_form(
                    step_id="select_device",
                    data_schema=self._selection_schema(),
                    errors={"base": "ownership_unavailable"},
                )
            if device_needs_restart(self.hass, dsn):
                return self.async_abort(reason="restart_required")
            if not any(
                flow["flow_id"] == self.flow_id
                for flow in self.hass.config_entries.flow.async_progress_by_handler(
                    DOMAIN, include_uninitialized=True
                )
            ):
                return self.async_abort(reason="reauth_cancelled")
            if claimants(self.hass, dsn) or pending_claim(
                self.hass, dsn, exclude=self.flow_id
            ):
                return self.async_abort(reason="already_configured")
            election = journal.get(device_unique_id(dsn))
            if (
                election
                and self.hass.config_entries.async_get_entry(election["owner"])
                is not None
            ):
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(device_unique_id(dsn))
            if device_needs_restart(self.hass, dsn):
                return self.async_abort(reason="restart_required")
            self._abort_if_unique_id_configured()
            token = reserve_enrollment(self.hass, dsn)
            return self.async_create_entry(
                title=title,
                data={
                    **self._settings,
                    CONF_DSN: dsn,
                    CONF_ENROLLMENT: token,
                    **({CONF_TRANSFER: True} if election else {}),
                },
            )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: DeLonghiDehumidifierConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return DeLonghiOptionsFlow()


class DeLonghiOptionsFlow(config_entries.OptionsFlow):
    """Retain the Configure entry point for existing installations."""

    _account_edit: _AccountEdit | None = None
    config_entry: DeLonghiDehumidifierConfigEntry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the same account settings as the reconfiguration flow."""
        if self._account_edit is None:
            self._account_edit = _AccountEdit(self.config_entry)
        return await self._account_edit.async_step(self, user_input)


class _AccountEdit:
    """A settings form stays bound to the entry as it was when opened."""

    def __init__(self, entry: DeLonghiDehumidifierConfigEntry) -> None:
        self.entry = entry
        self.data = entry.data
        self.options = entry.options
        self.unique_id = entry.unique_id
        self.disabled_by = entry.disabled_by

    def _settings_changed(self, hass: HomeAssistant) -> bool:
        entry = self.entry
        return (
            hass.config_entries.async_get_entry(entry.entry_id) is not entry
            or entry.data != self.data
            or entry.options != self.options
            or entry.unique_id != self.unique_id
            or entry.disabled_by != self.disabled_by
        )

    async def _abort_reason(self, hass: HomeAssistant) -> str | None:
        entry = self.entry
        try:
            owner = await read_device_owner(hass, self.unique_id)
        except ClaimJournalError:
            return "device_binding_required"
        if self._settings_changed(hass):
            return "settings_changed"
        dsn = self.data.get(CONF_DSN)
        if entry.entry_id in restart_required_entries(hass) or device_needs_restart(
            hass, dsn
        ):
            return "restart_required"
        if (
            owner != entry.entry_id
            or not usable_dsn(dsn)
            or self.unique_id != device_unique_id(dsn)
            or any(owner is not entry for owner in claimants(hass, dsn))
            or ir.async_get(hass).async_get_issue(
                DOMAIN, f"{ISSUE_PREFIX}{entry.entry_id}"
            )
        ):
            return "device_binding_required"
        return None

    async def async_step(
        self,
        flow: DeLonghiConfigFlow | DeLonghiOptionsFlow,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        if reason := await self._abort_reason(flow.hass):
            return flow.async_abort(reason=reason)
        errors: dict[str, str] = {}
        if user_input is not None:
            title, error = await _validate_device_settings(
                flow.hass, user_input, self.data[CONF_DSN]
            )
            if error is None:
                return await self._finish(flow, user_input, title)
            errors["base"] = error

        settings = {**self.data, **self.options}
        return flow.async_show_form(
            step_id="init" if isinstance(flow, OptionsFlow) else "reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LANGUAGE, default=settings[CONF_LANGUAGE]): str,
                    vol.Required(CONF_EMAIL, default=settings[CONF_EMAIL]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _finish(
        self,
        flow: DeLonghiConfigFlow | DeLonghiOptionsFlow,
        settings: dict[str, Any],
        title: str,
    ) -> ConfigFlowResult:
        if reason := await self._abort_reason(flow.hass):
            return flow.async_abort(reason=reason)
        entries = flow.hass.config_entries
        manager = entries.options if isinstance(flow, OptionsFlow) else entries.flow
        if not any(
            item["flow_id"] == flow.flow_id
            for item in manager.async_progress_by_handler(flow.handler)
        ):
            return flow.async_abort(reason="settings_changed")
        updates = {
            key: settings[key] for key in (CONF_LANGUAGE, CONF_EMAIL, CONF_PASSWORD)
        }
        options = {
            key: value for key, value in self.options.items() if key not in updates
        }
        # An eager update listener can unload itself before async_update_entry returns.
        reload_listener = _async_update_options in self.entry.update_listeners
        changed = entries.async_update_entry(
            self.entry, data={**self.data, **updates}, options=options
        )
        if not self.entry.disabled_by and (not changed or not reload_listener):
            entries.async_schedule_reload(self.entry.entry_id)
        if isinstance(flow, OptionsFlow):
            return flow.async_create_entry(title=title, data=options)
        return flow.async_abort(reason="reconfigure_successful")


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
