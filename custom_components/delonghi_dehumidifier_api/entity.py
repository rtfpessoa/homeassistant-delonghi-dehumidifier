"""Shared authentication availability for DeLonghi entities."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import Entity

from .client import (
    APIClient,
    InvalidCredentialsError,
    TransientCloudError,
    is_poll_connection_error,
)
from .const import DOMAIN

_COMMAND_ERROR_KEYS = frozenset(
    {
        "cloud_permission_denied",
        "command_connection_failed",
        "command_failed",
        "credentials_rejected",
        "device_removed",
        "ownership_recovery",
        "real_feel_enter_write_failed",
        "real_feel_mode_not_ready",
        "real_feel_read_failed",
        "real_feel_unsupported",
        "real_feel_write_failed",
        "restart_required",
        "settings_changing",
        "unsupported_mode",
    }
)


def command_error(err: Exception, fallback: str) -> HomeAssistantError:
    """Only copy recognized local reasons; exceptions, causes and placeholders may contain secrets."""
    key = fallback
    if (
        isinstance(err, HomeAssistantError)
        and err.translation_domain == DOMAIN
        and err.translation_key in _COMMAND_ERROR_KEYS
    ):
        key = err.translation_key
    kind = (
        ServiceValidationError
        if isinstance(err, ServiceValidationError)
        else HomeAssistantError
    )
    return kind(translation_domain=DOMAIN, translation_key=key)


def translate_command_errors[**P](
    action: Callable[P, Awaitable[None]],
) -> Callable[P, Coroutine[Any, Any, None]]:
    """Report action failures to Home Assistant without disclosing cloud data."""

    @wraps(action)
    async def run(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await action(*args, **kwargs)
        except InvalidCredentialsError:
            raise InvalidCredentialsError(
                translation_domain=DOMAIN, translation_key="credentials_rejected"
            ) from None
        except (aiohttp.ClientError, TimeoutError, TransientCloudError):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_connection_failed"
            ) from None
        except Exception as err:  # noqa: BLE001 - redact arbitrary command failures
            raise command_error(err, "command_failed") from None

    return run


class AuthAvailabilityEntity(Entity):
    """Expose confirmed credential rejection on all entities immediately."""

    client: APIClient
    _poll_generation: int
    _completed_poll_generation: int
    _action_updates = 0
    _action_refresh_generation: int | None = None
    _action_refresh_removed = False
    _action_refresh_handle: asyncio.Handle | None = None

    async def async_update(self) -> None:
        """Run a poll and retain action refreshes requested while it is running."""
        after_action = (
            self._action_refresh_generation is not None
            and self._action_refresh_generation == self.client.action_refresh.generation
            and not self.client.action_refresh.write_in_progress
        )
        self._action_refresh_generation = None
        self._action_updates += 1
        try:
            await self._async_update(after_action=after_action)
        finally:
            self._action_updates -= 1
            if self._action_refresh_generation is not None and not self._action_updates:
                self._schedule_action_refresh()

    async def _async_update(self, *, after_action: bool) -> None:
        """Update a platform's state."""
        raise NotImplementedError

    def _request_action_refresh(self, generation: int) -> None:
        self._action_refresh_generation = generation
        if not self._action_updates:
            self._schedule_action_refresh()

    def _schedule_action_refresh(self) -> None:
        if self._action_refresh_removed or self._action_refresh_handle is not None:
            return

        def refresh() -> None:
            self._action_refresh_handle = None
            if (
                self.client.action_refresh.write_in_progress
                or self._action_refresh_generation
                != self.client.action_refresh.generation
            ):
                # The later write schedules a new observation when it finishes.
                self._action_refresh_generation = None
            elif (
                not self._action_refresh_removed
                and self._action_refresh_generation is not None
            ):
                self.async_schedule_update_ha_state(force_refresh=True)

        # HA clears its staged-update flag after async_update returns.
        self._action_refresh_handle = self.hass.loop.call_soon(refresh)

    def _cancel_action_refresh(self) -> None:
        self._action_refresh_removed = True
        if self._action_refresh_handle is not None:
            self._action_refresh_handle.cancel()
            self._action_refresh_handle = None

    def _accept_poll(self, generation: int) -> bool:
        """Accept a result unless a newer poll has already completed."""
        if generation < self._completed_poll_generation:
            return False
        self._completed_poll_generation = generation
        return True

    def _poll_failed(
        self, generation: int, err: Exception, logger: logging.Logger
    ) -> None:
        """Mark a failed poll unavailable unless a newer poll has completed."""
        if not self._accept_poll(generation):
            return
        self._mark_poll_unavailable(err, logger)

    def _mark_poll_unavailable(self, err: Exception, logger: logging.Logger) -> None:
        """Preserve local value diagnostics; the shared read logs cloud outages."""
        if (
            self.available
            and not isinstance(err, InvalidCredentialsError)
            and not is_poll_connection_error(err)
        ):
            logger.warning("Unable to update entity (%s)", type(err).__name__)
        self._attr_available = False

    @property
    def available(self) -> bool:
        """Keep other platform-specific availability checks."""
        return (
            not self.client.closed
            and not self.client.invalid_credentials
            and super().available
        )

    async def async_added_to_hass(self) -> None:
        """Update the entity state when the shared client rejects credentials."""
        self._action_refresh_removed = False
        self._action_refresh_generation = None
        await super().async_added_to_hass()
        self.async_on_remove(
            self.client.subscribe_auth_failure(self.async_write_ha_state)
        )
        self.async_on_remove(self._cancel_action_refresh)
        self.async_on_remove(
            self.client.action_refresh.subscribe(self._request_action_refresh)
        )
