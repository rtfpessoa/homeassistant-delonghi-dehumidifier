"""Platform for sensor integration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import APIClient, OffOnStatus
from .const import DOMAIN
from .entity import AuthAvailabilityEntity
from .utils import fetch_device_info

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry[APIClient],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up current environment dehumidifier entity."""

    client = config_entry.runtime_data
    device_info = await fetch_device_info(client)
    device_dsn = await client.get_first_device()
    async_add_entities(
        [
            GenericOffOnSwitchSensor(
                device_dsn,
                device_info,
                client,
                "Eco Mode",
                await client.get_eco(),
                client.set_eco,
                client.get_eco,
            ),
            GenericOffOnSwitchSensor(
                device_dsn,
                device_info,
                client,
                "Swing Mode",
                await client.get_swing(),
                client.set_swing,
                client.get_swing,
            ),
        ]
    )


class GenericOffOnSwitchSensor(AuthAvailabilityEntity, SwitchEntity):
    """Switch entity representing a generic on/off switch sensor for a Dehumidifier."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        device_dsn: str,
        device_info: DeviceInfo,
        client: APIClient,
        type_name: str,
        status: OffOnStatus | None,
        set_status: Callable[[OffOnStatus], Coroutine[Any, Any, dict[Any, Any]]],
        get_status: Callable[[], Coroutine[Any, Any, OffOnStatus | None]] | None = None,
    ) -> None:
        """Initialize."""
        self.client = client
        self._is_on = None if status is None else status == OffOnStatus.ON
        self._attr_unique_id = (
            f"{DOMAIN}_{device_dsn}_{re.sub(r'\s+', '_', type_name.lower())}_switch"
        )
        self._attr_translation_key = re.sub(r"\s+", "_", type_name.lower())
        self._attr_device_info = device_info
        self._set_status = set_status
        self._get_status = get_status
        self._command_lock = asyncio.Lock()
        self._command_generation = 0
        self._last_successful_command_generation = 0
        self._last_command_completed = None
        self._skip_next_poll = False
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    @property
    def is_on(self):
        """If the switch is currently on or off."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        _LOGGER.debug("Turning on %s", self._attr_unique_id)
        async with self._command_lock:
            # A pending skip belongs to an earlier command.
            self._skip_next_poll = False
            self._command_generation += 1
            self.client.action_refresh.write_started()
            try:
                await self._set_status(OffOnStatus.ON)
            finally:
                self.client.action_refresh.write_finished()
            self._last_successful_command_generation = self._command_generation
            self._last_command_completed = time.monotonic()
            self._attr_available = True
            self._is_on = True
            self._skip_next_poll = True

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        _LOGGER.debug("Turning off %s", self._attr_unique_id)
        async with self._command_lock:
            # A pending skip belongs to an earlier command.
            self._skip_next_poll = False
            self._command_generation += 1
            self.client.action_refresh.write_started()
            try:
                await self._set_status(OffOnStatus.OFF)
            finally:
                self.client.action_refresh.write_finished()
            self._last_successful_command_generation = self._command_generation
            self._last_command_completed = time.monotonic()
            self._attr_available = True
            self._is_on = False
            self._skip_next_poll = True

    async def _async_update(self, *, after_action: bool) -> None:
        """Read cloud state unless its snapshot predates the last command."""
        if self._get_status is None:
            return
        if self._command_lock.locked():
            return
        # Home Assistant refreshes a switch immediately after a command.
        # Keep the accepted state and avoid waiting for an unrelated read.
        if self._skip_next_poll and not after_action:
            self._skip_next_poll = False
            return
        if after_action:
            self._skip_next_poll = False

        generation = self._command_generation
        try:
            status = await self._get_status()
        except Exception as err:  # noqa: BLE001
            if generation < self._last_successful_command_generation:
                # HA skips its automatic refresh while this poll is running.
                if not self._command_lock.locked():
                    self._skip_next_poll = False
                return
            # A failed poll must not leave an old state displayed.
            if self.available:
                _LOGGER.warning(
                    "Unable to update %s (%s)", self.name, type(err).__name__
                )
            self._attr_available = False
            return

        if generation != self._command_generation:
            # HA skips its automatic refresh while this poll is running.
            if not self._command_lock.locked():
                self._skip_next_poll = False
            return

        snapshot_started = self.client.device_properties_request_started
        if self._last_command_completed is not None and (
            snapshot_started is None or snapshot_started <= self._last_command_completed
        ):
            return

        self._attr_available = True
        self._is_on = None if status is None else status == OffOnStatus.ON
