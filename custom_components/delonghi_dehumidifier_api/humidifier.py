"""Adds dehumidifer entity for each dehumidifer appliance."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.humidifier import HumidifierDeviceClass, HumidifierEntity
from homeassistant.components.humidifier.const import HumidifierEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import (
    MODE_BY_NAME,
    MODE_BY_VALUE,
    STATUS_BY_VALUE,
    APIClient,
    Mode,
    Status,
)
from .const import DOMAIN
from .entity import AuthAvailabilityEntity
from .utils import fetch_device_info

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)


def _int_property(properties: list[dict], name: str) -> int | None:
    """Read one integer from a single cloud snapshot."""
    value = next(
        (prop.get("value") for prop in properties if prop.get("name") == name), None
    )
    return None if value is None else int(value)


def _snapshot_values(
    properties: list[dict],
) -> tuple[str | None, int | None, int | None, bool | None]:
    """Interpret every humidifier attribute from the same complete snapshot."""
    mode = MODE_BY_VALUE.get(_int_property(properties, "device_mode"))
    status = STATUS_BY_VALUE.get(_int_property(properties, "device_status"))
    return (
        mode.name if mode is not None else None,
        _int_property(properties, "humidity_setpoint"),
        _int_property(properties, "current_humidity"),
        None if status is None else status == Status.ON,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry[APIClient],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up current environment dehumidifier entity."""

    client = config_entry.runtime_data
    device_info = await fetch_device_info(client)
    device_dsn = await client.get_first_device()
    async_add_entities([DehumidifierEntity(device_dsn, device_info, client)])


class DehumidifierEntity(AuthAvailabilityEntity, HumidifierEntity):
    """Dehumidifer entity for DeLonghi dehumidifier."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_max_humidity = 100
    _attr_min_humidity = 0
    _attr_supported_features = HumidifierEntityFeature.MODES
    # Keep Home Assistant's instance-attribute typing and the existing shared list.
    _attr_available_modes = [  # noqa: RUF012
        Mode.DEHUMIDIFY.name,
        Mode.DRY_CLOTHES.name,
        Mode.PURIFIER.name,
    ]

    def __init__(
        self,
        device_dsn: str,
        device_info: DeviceInfo,
        client: APIClient,
    ) -> None:
        """Initialize."""
        self.client = client
        self._attr_unique_id = f"{DOMAIN}_{device_dsn}_dehumidifier"
        self._attr_name = "Unit"
        self._attr_device_info = device_info
        self._poll_generation = 0
        self._completed_poll_generation = 0
        self._command_lock = asyncio.Lock()
        self._command_generation = 0
        self._refresh_handle: asyncio.Handle | None = None
        self._removed = False
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_refresh)
        await self.async_update()

    def _cancel_refresh(self) -> None:
        """Do not schedule follow-up work after the entity is removed."""
        self._removed = True
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None

    def _schedule_refresh(self) -> None:
        """Refresh after HA has released the currently staged update."""
        if self._removed or self._refresh_handle is not None:
            return

        def refresh() -> None:
            self._refresh_handle = None
            if not self._removed:
                self.async_schedule_update_ha_state(force_refresh=True)

        self._refresh_handle = self.hass.loop.call_soon(refresh)

    async def _command(self, send: Callable[[], Awaitable[dict]]) -> None:
        """Serialize writes and request an observation only after acceptance."""
        async with self._command_lock:
            await send()
            self._command_generation += 1
            self.client.invalidate_properties()

    async def async_update(self) -> None:
        """Handle the event when the device status changes."""
        _LOGGER.debug("Updating %s", self._attr_unique_id)
        self._poll_generation += 1
        generation = self._poll_generation
        command_generation = self._command_generation
        try:
            values = _snapshot_values(await self.client.get_properties())
        except Exception as err:  # noqa: BLE001
            if command_generation == self._command_generation:
                self._poll_failed(generation, err, _LOGGER)
            return
        finally:
            if command_generation != self._command_generation:
                self._schedule_refresh()
        if command_generation != self._command_generation:
            return
        if not self._accept_poll(generation):
            return
        (
            self._attr_mode,
            self._attr_target_humidity,
            self._attr_current_humidity,
            self._attr_is_on,
        ) = values
        self._attr_available = True
        _LOGGER.debug(
            "Updated %s with %s",
            self._attr_unique_id,
            {
                "mode": self._attr_mode,
                "target_humidity": self._attr_target_humidity,
                "current_humidity": self._attr_current_humidity,
                "is_on": self._attr_is_on,
            },
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        _LOGGER.debug("Turning on %s", self._attr_unique_id)
        await self._command(lambda: self.client.set_status(Status.ON))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        _LOGGER.debug("Turning off %s", self._attr_unique_id)
        await self._command(lambda: self.client.set_status(Status.OFF))

    async def async_set_mode(self, mode: str) -> None:
        """Set new target mode."""
        _LOGGER.debug("Setting mode %s mode to %s", self._attr_unique_id, mode)
        await self._command(lambda: self.client.set_mode(MODE_BY_NAME.get(mode)))

    async def async_set_humidity(self, humidity: int) -> None:
        """Set new target humidity."""
        _LOGGER.debug("Setting humidity %s mode to %s", self._attr_unique_id, humidity)
        await self._command(lambda: self.client.set_humidity(humidity))
