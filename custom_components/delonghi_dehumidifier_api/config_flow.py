"""Config flow for the DeLonghi Dehumidifier integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import device_registry as dr

from .client import (
    APIClient,
    DeviceNotAvailableError,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
)
from .const import DOMAIN, RUNTIME_REAUTH_ID
from .identity import (
    CONF_DSN,
    CONF_ENROLLMENT,
    CONF_TRANSFER,
    ClaimJournalError,
    binding_lock,
    claimants,
    device_needs_restart,
    device_unique_id,
    pending_claim,
    reload_journal,
    reserve_enrollment,
    runtime_is_current,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LANGUAGE, default="en"): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(
    hass: HomeAssistant, data: dict[str, Any], dsn: str
) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    session = aiohttp_client.async_get_clientsession(hass)
    client = APIClient(
        session, data[CONF_LANGUAGE], data[CONF_EMAIL], data[CONF_PASSWORD]
    )
    try:
        authenticated = await client.authenticate()
        if not authenticated:
            raise InvalidAuth
        await client.verify_device(dsn)
        return {"title": await client.get_product_name()}
    except InvalidCredentialsError as err:
        raise InvalidAuth from err
    finally:
        await client.async_shutdown()


class DeLonghiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DeLonghi Dehumidifier."""

    VERSION = 2

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
        entry: ConfigEntry,
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

    async def async_step_select_device(self, user_input=None) -> ConfigFlowResult:
        """Ask the user to select a discovered identity explicitly."""
        if user_input is not None:
            return await self._select_device(user_input[CONF_DSN])
        return self.async_show_form(
            step_id="select_device",
            data_schema=self._selection_schema(),
        )

    def _selection_schema(self):
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
        try:
            info = await validate_input(self.hass, self._settings, dsn)
        except InvalidAuth:
            error = "invalid_auth"
        except (aiohttp.ClientError, TimeoutError, TransientCloudError):
            error = "cannot_connect"
        except NoDevicesError:
            error = "no_devices"
        except DeviceNotAvailableError:
            error = "device_not_available"
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected selection failure (%s)", type(err).__name__)
            error = "unknown"
        else:
            return await self._enroll_selected_device(dsn, info["title"])
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
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return DeLonghiOptionsFlow()


class DeLonghiOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            original_data, original_options = dict(self.config_entry.data), dict(
                self.config_entry.options
            )
            try:
                info = await validate_input(
                    self.hass, user_input, self.config_entry.data[CONF_DSN]
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, TransientCloudError):
                errors["base"] = "cannot_connect"
            except NoDevicesError:
                errors["base"] = "no_devices"
            except DeviceNotAvailableError:
                errors["base"] = "device_not_available"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Unexpected options failure (%s)", type(err).__name__)
                errors["base"] = "unknown"
            else:
                if (
                    self.hass.config_entries.async_get_entry(self.config_entry.entry_id)
                    is not self.config_entry
                    or self.config_entry.data != original_data
                    or self.config_entry.options != original_options
                ):
                    return self.async_abort(reason="reauth_settings_changed")
                return self.async_create_entry(
                    title=info["title"],
                    data={**self.config_entry.options, **user_input},
                )

        settings = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LANGUAGE, default=settings[CONF_LANGUAGE]): str,
                    vol.Required(CONF_EMAIL, default=settings[CONF_EMAIL]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
