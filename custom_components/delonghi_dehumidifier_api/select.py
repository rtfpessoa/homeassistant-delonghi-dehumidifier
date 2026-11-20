"""Platform for select integration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeLonghiDehumidifierConfigEntry
from .client import APIClient, Mode, parse_integer_state, property_records
from .const import DOMAIN
from .entity import AuthAvailabilityEntity, translate_command_errors
from .utils import fetch_device_info

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
# The client coalesces reads; this entity serializes writes to rotation_speed.
PARALLEL_UPDATES = 0

_SPEED_OPTIONS = ["1", "2", "3"]
_VALID_SPEEDS = {1, 2, 3}
_COMMAND_SETTLE_SECONDS = 15.0


def _snapshot_int(properties: list[object], name: str) -> int | None:
    """Read an integer property from one cloud property snapshot."""
    value = next(
        (
            prop.get("value")
            for prop in property_records(properties)
            if prop.get("name") == name
        ),
        None,
    )
    return parse_integer_state(value)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeLonghiDehumidifierConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DeLonghi dehumidifier select entities."""
    client = config_entry.runtime_data
    device_info = await fetch_device_info(client)
    device_dsn = await client.get_first_device()
    async_add_entities(
        [FanSpeedSelect(device_dsn, device_info, client)],
        True,
    )


class FanSpeedSelect(AuthAvailabilityEntity, SelectEntity):
    """Select entity to control the DeLonghi fan speed in Purifier mode."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_icon = "mdi:fan"
    _attr_translation_key = "fan_speed"

    def __init__(
        self,
        device_dsn: str,
        device_info: DeviceInfo,
        client: APIClient,
    ) -> None:
        """Initialize the fan speed select entity."""
        self.client = client
        self._attr_unique_id = f"{DOMAIN}_{device_dsn}_fan_speed_select"
        self._attr_device_info = device_info
        self._attr_options = list(_SPEED_OPTIONS)
        self._attr_current_option: str | None = None
        self._is_purifier_mode = False
        self._command_lock = asyncio.Lock()
        self._command_generation = 0
        self._last_successful_command_generation = 0
        self._last_command_completed: float | None = None
        self._skip_next_poll = False

    @property
    def available(self) -> bool:
        """Fan speed is only controllable when the appliance is in Purifier mode."""
        return self._is_purifier_mode and super().available

    @translate_command_errors
    async def async_select_option(self, option: str) -> None:
        """Change the fan speed when in Purifier mode."""
        if option not in _SPEED_OPTIONS:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="command_failed"
            )
        speed = int(option)
        async with self._command_lock:
            if not self._is_purifier_mode:
                properties = await self.client.get_properties()
                device_mode = _snapshot_int(properties, "device_mode")
                if device_mode != Mode.PURIFIER.value:
                    self._is_purifier_mode = False
                    self._attr_current_option = None
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="fan_speed_purifier_only",
                    )

            self._skip_next_poll = False
            self._command_generation += 1
            self.client.action_refresh.write_started()
            try:
                await self.client.set_rotation_speed(speed)
            finally:
                self.client.action_refresh.write_finished()
            self._last_successful_command_generation = self._command_generation
            self._last_command_completed = time.monotonic()
            self._is_purifier_mode = True
            self._attr_available = True
            self._attr_current_option = option
            self._skip_next_poll = True

    async def _async_update(self, *, after_action: bool) -> None:
        """Read cloud fan speed unless its snapshot predates the last command."""
        if self._command_lock.locked():
            return
        if self._skip_next_poll and not after_action:
            self._skip_next_poll = False
            return
        if after_action:
            self._skip_next_poll = False

        generation = self._command_generation
        try:
            properties = await self.client.get_properties()
            device_mode = _snapshot_int(properties, "device_mode")
            current_speed = _snapshot_int(properties, "current_speed")
            rotation_speed = _snapshot_int(properties, "rotation_speed")
        except Exception as err:  # noqa: BLE001
            if generation < self._last_successful_command_generation:
                if not self._command_lock.locked():
                    self._skip_next_poll = False
                return
            self._mark_poll_unavailable(err, _LOGGER)
            return

        if generation != self._command_generation:
            if not self._command_lock.locked():
                self._skip_next_poll = False
            return

        snapshot_started = self.client.device_properties_request_started
        if self._last_command_completed is not None and (
            snapshot_started is None or snapshot_started <= self._last_command_completed
        ):
            return

        is_purifier = device_mode == Mode.PURIFIER.value
        self._is_purifier_mode = is_purifier
        self._attr_available = True

        if not is_purifier:
            self._attr_current_option = None
            return

        recently_commanded = (
            self._last_command_completed is not None
            and (time.monotonic() - self._last_command_completed)
            < _COMMAND_SETTLE_SECONDS
        )
        if recently_commanded and rotation_speed in _VALID_SPEEDS:
            selected = str(rotation_speed)
        elif current_speed in _VALID_SPEEDS:
            selected = str(current_speed)
        elif rotation_speed in _VALID_SPEEDS:
            selected = str(rotation_speed)
        else:
            selected = None

        self._attr_current_option = selected
