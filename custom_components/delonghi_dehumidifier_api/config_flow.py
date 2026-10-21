"""Config flow for the DeLonghi Dehumidifier integration."""

from __future__ import annotations

import logging
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

from .client import APIClient, InvalidCredentialsError
from .const import DOMAIN, RUNTIME_REAUTH_ID

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LANGUAGE, default="en"): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    session = aiohttp_client.async_get_clientsession(hass)
    client = APIClient(
        session, data[CONF_LANGUAGE], data[CONF_EMAIL], data[CONF_PASSWORD]
    )
    try:
        authenticated = await client.authenticate()
    except InvalidCredentialsError as err:
        raise InvalidAuth from err
    if not authenticated:
        raise InvalidAuth

    product_name = await client.get_product_name()

    return {"title": product_name}


class DeLonghiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DeLonghi Dehumidifier."""

    VERSION = 1

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauthentication for the existing entry."""
        runtime_id = entry_data.get(RUNTIME_REAUTH_ID)
        if runtime_id is not None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if (
                entry is None
                or entry.state is not ConfigEntryState.LOADED
                or entry.runtime_data.reauth_id != runtime_id
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
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauthentication")
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
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
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
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

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
