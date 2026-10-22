"""Binary sensors for DeLonghi dehumidifiers."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeLonghiDehumidifierConfigEntry
from .client import APIClient
from .const import DOMAIN
from .entity import AuthAvailabilityEntity
from .utils import fetch_device_info

SCAN_INTERVAL = timedelta(minutes=1)
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeLonghiDehumidifierConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the observed device alarm."""
    client = entry.runtime_data
    device_info = await fetch_device_info(client)
    device_dsn = await client.get_first_device()
    async_add_entities([DeviceAlarm(client, device_dsn, device_info)], True)


class DeviceAlarm(AuthAvailabilityEntity, BinarySensorEntity):
    """Report known device alarm values without attributing a cause."""

    _attr_has_entity_name = True
    _attr_translation_key = "device_alarm"
    _attr_should_poll = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_available = True

    def __init__(self, client: APIClient, device_dsn: str, device_info: DeviceInfo):
        """Initialize the alarm entity."""
        self.client = client
        self._attr_unique_id = f"{DOMAIN}_{device_dsn}_device_alarm_binary_sensor"
        self._attr_device_info = device_info
        self._attr_is_on = None

    async def async_update(self) -> None:
        """Interpret alarm values only for the observed model."""
        try:
            properties = await self.client.get_properties()
        except Exception as err:  # noqa: BLE001
            # Any fetch failure must not leave a stale alarm state visible.
            if self._attr_available:
                _LOGGER.warning(
                    "Unable to update device alarm (%s)", type(err).__name__
                )
            self._attr_available = False
            return

        self._attr_available = True
        self._attr_is_on = None
        if not isinstance(properties, list):
            return

        model = next(
            (
                prop.get("value")
                for prop in properties
                if isinstance(prop, dict) and prop.get("name") == "appliance_model"
            ),
            None,
        )
        if model != "DDSX220":
            return

        value = next(
            (
                prop.get("value")
                for prop in properties
                if isinstance(prop, dict) and prop.get("name") == "alarm_state"
            ),
            None,
        )
        self._attr_is_on = (
            {0: False, 3: True}.get(value) if type(value) is int else None
        )
