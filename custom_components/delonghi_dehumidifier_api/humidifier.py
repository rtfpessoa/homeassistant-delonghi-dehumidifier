"""Adds dehumidifer entity for each dehumidifer appliance."""

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.humidifier import HumidifierDeviceClass, HumidifierEntity
from homeassistant.components.humidifier.const import HumidifierEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import MODE_BY_NAME, APIClient, Mode, Status
from .const import DOMAIN
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
    async_add_entities([DehumidifierEntity(device_dsn, device_info, client)])


class DehumidifierEntity(HumidifierEntity):
    """Dehumidifer entity for DeLonghi dehumidifier."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_max_humidity = 100
    _attr_min_humidity = 0
    _attr_supported_features = HumidifierEntityFeature.MODES
    _attr_available_modes = [
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
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    async def async_added_to_hass(self) -> None:
        await self.async_update()

    async def async_update(self) -> None:
        """Handle the event when the device status changes."""
        _LOGGER.debug("Updating %s", self._attr_unique_id)
        device_mode = await self.client.get_device_mode()
        self._attr_mode = device_mode.name
        self._attr_target_humidity = await self.client.get_humidity_setpoint()
        self._attr_current_humidity = await self.client.get_current_humidity()
        device_status = await self.client.get_device_status()
        self._attr_is_on = device_status == Status.ON
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
        await self.client.set_status(Status.ON)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        _LOGGER.debug("Turning off %s", self._attr_unique_id)
        await self.client.set_status(Status.OFF)

    async def async_set_mode(self, mode: str) -> None:
        """Set new target mode."""
        _LOGGER.debug("Setting mode %s mode to %s", self._attr_unique_id, mode)
        await self.client.set_mode(MODE_BY_NAME.get(mode))

    async def async_set_humidity(self, humidity: int) -> None:
        """Set new target humidity."""
        _LOGGER.debug("Setting humidity %s mode to %s", self._attr_unique_id, humidity)
        await self.client.set_humidity(humidity)
