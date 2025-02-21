"""Platform for sensor integration."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
from enum import Enum
import logging
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import APIClient, FilterStatus, Mode, OffOnStatus, Status
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
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Current Humidity",
                client.get_current_humidity,
                SensorDeviceClass.HUMIDITY,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=PERCENTAGE,
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Target Humidity",
                client.get_humidity_setpoint,
                SensorDeviceClass.HUMIDITY,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=PERCENTAGE,
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Current Speed",
                client.get_current_speed,
                SensorDeviceClass.SPEED,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Filter Status",
                get_and_extract_enum_name(client.get_filter_status),
                SensorDeviceClass.ENUM,
                options=[status.name for status in FilterStatus],
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Room Temperature",
                client.get_room_temp,
                SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Heat Exchanger Temperature",
                client.get_heat_exchanger_temp,
                SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Device Mode",
                get_and_extract_enum_name(client.get_device_mode),
                SensorDeviceClass.ENUM,
                options=[mode.name for mode in Mode],
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Device Status",
                get_and_extract_enum_name(client.get_device_status),
                SensorDeviceClass.ENUM,
                options=[mode.name for mode in Status],
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Eco Mode",
                get_and_extract_enum_name(client.get_eco),
                SensorDeviceClass.ENUM,
                options=[status.name for status in OffOnStatus],
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Swing Mode",
                get_and_extract_enum_name(client.get_swing),
                SensorDeviceClass.ENUM,
                options=[status.name for status in OffOnStatus],
            ),
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Filter Change Alarm",
                get_and_extract_enum_name(client.get_filter_change_alarm),
                SensorDeviceClass.ENUM,
                options=[status.name for status in OffOnStatus],
            ),
            # TODO: Fix units
            GenericSensor(
                device_dsn,
                device_info,
                client,
                "Filter Life",
                client.get_filter_life,
                SensorDeviceClass.DURATION,
                state_class=SensorStateClass.MEASUREMENT,
                unit_of_measurement=UnitOfTime.DAYS,
            ),
        ]
    )


def get_and_extract_enum_name(
    get_current_value: Callable[[], Coroutine[Any, Any, Enum]],
) -> Callable[[], Coroutine[Any, Any, str]]:
    """Wrap a Callable that returns an Awaitable[Enum], and returns a Callable returning an Awaitable[str] with the enum name."""

    async def wrapper() -> str:
        mode = await get_current_value()
        return mode.name

    return wrapper


class GenericSensor(SensorEntity):
    """Current environment humidity sensor."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        device_dsn: str,
        device_info: DeviceInfo,
        client: APIClient,
        type_name: str,
        get_value: Callable[[], Coroutine[Any, Any, Any]],
        device_class: SensorDeviceClass,
        state_class: SensorStateClass | None = None,
        unit_of_measurement: str | None = None,
        options: list[str] | None = None,
    ) -> None:
        """Initialize."""
        self.client = client
        self._attr_unique_id = (
            f"{DOMAIN}_{device_dsn}_{re.sub(r'\s+', '_', type_name.lower())}_sensor"
        )
        self._attr_name = type_name
        self._attr_device_info = device_info
        self._get_value = get_value
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_options = options
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    async def async_added_to_hass(self) -> None:
        """Call when the entity is added to Home Assistant."""
        await self.async_update()

    async def async_update(self):
        """Update the sensor's state.

        This method fetches the current humidity from the client and updates the
        sensor's native value with the retrieved humidity. It also logs the current
        humidity value for debugging purposes.

        Returns:
            None

        """
        self._attr_native_value = await self._get_value()
        _LOGGER.debug(
            "Updated %s with %s",
            self._attr_unique_id,
            {
                "value": self._attr_native_value,
            },
        )
