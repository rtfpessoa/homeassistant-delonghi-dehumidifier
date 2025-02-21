"""Platform for sensor integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import APIClient, OffOnStatus
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
    async_add_entities(
        [
            GenericOffOnSwitchSensor(
                device_dsn,
                device_info,
                client,
                "Eco Mode",
                await client.get_eco(),
                client.set_eco,
            ),
            GenericOffOnSwitchSensor(
                device_dsn,
                device_info,
                client,
                "Swing Mode",
                await client.get_swing(),
                client.set_swing,
            ),
        ]
    )


class GenericOffOnSwitchSensor(SwitchEntity):
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
        status: OffOnStatus,
        set_status: Callable[[OffOnStatus], Coroutine[Any, Any, dict[Any, Any]]],
    ) -> None:
        """Initialize."""
        self.client = client
        self._is_on = status == OffOnStatus.ON
        self._attr_unique_id = (
            f"{DOMAIN}_{device_dsn}_{re.sub(r'\s+', '_', type_name.lower())}_switch"
        )
        self._attr_name = type_name
        self._attr_device_info = device_info
        self._set_status = set_status
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    @property
    def is_on(self):
        """If the switch is currently on or off."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        _LOGGER.debug("Turning on %s", self._attr_unique_id)
        await self._set_status(OffOnStatus.ON)
        self._is_on = True

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        _LOGGER.debug("Turning off %s", self._attr_unique_id)
        await self._set_status(OffOnStatus.OFF)
        self._is_on = False
