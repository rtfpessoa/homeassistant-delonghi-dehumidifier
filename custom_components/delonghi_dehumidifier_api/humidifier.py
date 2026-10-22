"""Adds dehumidifer entity for each dehumidifer appliance."""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.humidifier import HumidifierDeviceClass, HumidifierEntity
from homeassistant.components.humidifier.const import HumidifierEntityFeature
from homeassistant.components.logbook import async_log_entry
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.translation import async_get_translations

from .client import (
    MODE_BY_NAME,
    MODE_BY_VALUE,
    STATUS_BY_VALUE,
    APIClient,
    InvalidCredentialsError,
    Mode,
    Status,
)
from .const import DOMAIN, REAL_FEEL_ACTIVATION_PAYLOAD, REAL_FEEL_DEACTIVATION_PAYLOAD
from .entity import AuthAvailabilityEntity
from .real_feel import property_value, supports_real_feel
from .utils import fetch_device_info

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
_PENDING_SECONDS = 120
_PENDING_REFRESH_INTERVAL = 15
_PENDING_REFRESH_LIMIT = 4
_ORDINARY_MODES = [Mode.DEHUMIDIFY.name, Mode.DRY_CLOTHES.name, Mode.PURIFIER.name]


def _tested_device_mode(properties: list[dict]) -> str | None:
    """Estimate the tested device's mode from its cloud command echo."""
    if not supports_real_feel(properties):
        return None
    mode = property_value(properties, "device_mode")
    if type(mode) is not int:
        return None
    if mode in (2, 3):
        return MODE_BY_VALUE[mode].name
    if mode != 1:
        return None
    command = property_value(properties, "activate_realfeel")
    if type(command) is not str:
        return None
    if command == REAL_FEEL_ACTIVATION_PAYLOAD:
        return Mode.REAL_FEEL.name
    if command == REAL_FEEL_DEACTIVATION_PAYLOAD:
        return Mode.DEHUMIDIFY.name
    return None


def _int_property(properties: list[dict], name: str) -> int | None:
    """Read one integer from a single cloud snapshot."""
    value = next(
        (prop.get("value") for prop in properties if prop.get("name") == name), None
    )
    return None if value is None else int(value)


def _snapshot_values(
    properties: list[dict], *, tested_device: bool = False
) -> tuple[str | None, int | None, int | None, bool | None]:
    """Interpret every humidifier attribute from the same complete snapshot."""
    if tested_device:
        mode = _tested_device_mode(properties)
    else:
        legacy_mode = MODE_BY_VALUE.get(_int_property(properties, "device_mode"))
        mode = legacy_mode.name if legacy_mode is not None else None
    status = STATUS_BY_VALUE.get(_int_property(properties, "device_status"))
    return (
        mode,
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
    _attr_translation_key = "unit"
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
        self._attr_device_info = device_info
        self._poll_generation = 0
        self._completed_poll_generation = 0
        self._refresh_handle: asyncio.Handle | None = None
        self._removed = False
        self._lifetime_generation = 0
        self._tested_device = False
        self._last_observed_mode: str | None = None
        self._activity_mode: str | None = None
        self._activity_translations: dict[str, dict[str, str]] = {}
        self._pending_mode: str | None = None
        self._pending_source: str | None = None
        self._pending_earlier_modes: set[str] = set()
        self._pending_deadline: float | None = None
        self._pending_timer: asyncio.TimerHandle | None = None
        self._pending_refresh_timer: asyncio.TimerHandle | None = None
        self._pending_refresh_remaining = 0
        self._pending_generation = 0
        _LOGGER.debug("Initialized %s", self._attr_unique_id)

    async def async_added_to_hass(self) -> None:
        self._removed = False
        await super().async_added_to_hass()
        for language in ("en", "de"):
            self._activity_translations[language] = await async_get_translations(
                self.hass, language, "common", [DOMAIN]
            )
        self.async_on_remove(self._cancel_refresh)
        self.async_on_remove(
            self.client.humidifier_commands.subscribe_refresh(self._schedule_refresh)
        )
        await self.async_update()

    def _cancel_refresh(self) -> None:
        """Do not schedule follow-up work after the entity is removed."""
        self._removed = True
        self._lifetime_generation += 1
        self._clear_pending()
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

    async def _async_update(self, *, after_action: bool) -> None:
        """Handle the event when the device status changes."""
        _LOGGER.debug("Updating %s", self._attr_unique_id)
        self._poll_generation += 1
        generation = self._poll_generation
        command_generation = self.client.humidifier_commands.generation
        try:
            properties = await self.client.get_properties()
            supported = supports_real_feel(properties)
            values = _snapshot_values(
                properties, tested_device=self._tested_device or supported
            )
        except Exception as err:  # noqa: BLE001
            if command_generation == self.client.humidifier_commands.generation:
                self._poll_failed(generation, err, _LOGGER)
            return
        finally:
            if command_generation != self.client.humidifier_commands.generation:
                self._schedule_refresh()
        if command_generation != self.client.humidifier_commands.generation:
            return
        if not self._accept_poll(generation):
            return
        self._apply_snapshot(values, supported)

    def _apply_snapshot(
        self,
        values: tuple[str | None, int | None, int | None, bool | None],
        supported: bool,
    ) -> None:
        """Publish the accepted snapshot and its tested-device mode estimate."""
        self._tested_device = self._tested_device or supported
        self._attr_available_modes = (
            [*_ORDINARY_MODES, Mode.REAL_FEEL.name] if supported else _ORDINARY_MODES
        )
        mode = values[0]
        if self._tested_device:
            self._last_observed_mode = mode
            mode = self._mode_with_pending(mode)
        (
            _,
            self._attr_target_humidity,
            self._attr_current_humidity,
            self._attr_is_on,
        ) = values
        self._attr_mode = mode
        self._attr_available = True
        self._record_mode_activity(mode)
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

    def _record_mode_activity(self, mode: str | None) -> None:
        """Record changes involving Real Feel only after an accepted cloud read."""
        if (
            self._removed
            or not self.available
            or mode not in self._attr_available_modes
        ):
            return
        previous = self._activity_mode
        self._activity_mode = mode
        if (
            previous is None
            or previous == mode
            or Mode.REAL_FEEL.name not in (previous, mode)
        ):
            return
        language = (self.hass.config.language or "en").split("-")[0]
        translations = self._activity_translations.get(
            language, self._activity_translations["en"]
        )
        key = f"component.{DOMAIN}.common.mode_activity_{mode.lower()}"
        if message := translations.get(key):
            async_log_entry(
                self.hass, self.name or "De'Longhi", message, entity_id=self.entity_id
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        _LOGGER.debug("Turning on %s", self._attr_unique_id)
        await self.client.humidifier_commands.run(
            lambda: self.client.set_status(Status.ON)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        _LOGGER.debug("Turning off %s", self._attr_unique_id)
        await self.client.humidifier_commands.run(
            lambda: self.client.set_status(Status.OFF)
        )

    async def async_set_mode(self, mode: str) -> None:
        """Set new target mode."""
        _LOGGER.debug("Setting mode %s mode to %s", self._attr_unique_id, mode)
        target = MODE_BY_NAME.get(mode)
        if target is None:
            raise ServiceValidationError(f"Unsupported dehumidifier mode: {mode!r}")
        tested_device = False
        source_mode: str | None = None
        lifetime = self._lifetime_generation

        async def guard() -> None:
            nonlocal tested_device, source_mode
            tested_device = self._tested_device
            if tested_device:
                self._ensure_not_removed(lifetime)
            if tested_device and target == Mode.DEHUMIDIFY:
                source_mode = await self._fresh_dehumidify_source(lifetime)

        async def send() -> dict:
            if not tested_device:
                return await self.client.set_mode(target)
            return await self._send_tested_mode(target, source_mode, lifetime)

        await self.client.humidifier_commands.run(send, guard=guard)

    def _ensure_not_removed(self, lifetime: int) -> None:
        """Reject a queued request after the entity has been unloaded."""
        if self._removed or self._lifetime_generation != lifetime:
            raise ServiceValidationError(
                "The device is no longer available. Reload the integration and try again.",
                translation_domain=DOMAIN,
                translation_key="device_removed",
            )

    @staticmethod
    def _mode_not_ready() -> ServiceValidationError:
        return ServiceValidationError(
            "The device mode is not ready. Wait for the De'Longhi app to sync, then try again.",
            translation_domain=DOMAIN,
            translation_key="real_feel_mode_not_ready",
        )

    async def _fresh_dehumidify_source(self, lifetime: int) -> str:
        """Choose the Dehumidify route using a post-lock cloud snapshot."""
        self._ensure_not_removed(lifetime)
        self._expire_due_pending()
        read_started = self.hass.loop.time()
        deadline = self._pending_deadline
        self.client.invalidate_properties()
        try:
            properties = await self.client.get_properties()
        except InvalidCredentialsError:
            raise
        except Exception:  # noqa: BLE001 - do not expose cloud response details
            raise HomeAssistantError(
                "Unable to check the current device data. Check the connection and try again.",
                translation_domain=DOMAIN,
                translation_key="real_feel_read_failed",
            ) from None
        self._ensure_not_removed(lifetime)
        if deadline is not None and read_started < deadline <= self.hass.loop.time():
            self._expire_due_pending()
            raise self._mode_not_ready()
        if not supports_real_feel(properties):
            raise ServiceValidationError(
                "This request requires a DDSX220 with firmware 1.0. "
                "Check the device information and reload the integration.",
                translation_domain=DOMAIN,
                translation_key="real_feel_unsupported",
            )
        current = _tested_device_mode(properties)
        if self._mode_with_pending(current) is None or current is None:
            raise self._mode_not_ready()
        return current

    async def _send_tested_mode(
        self, target: Mode, source: str | None, lifetime: int
    ) -> dict:
        """Attempt one mode command and keep conflicting echoes pending."""
        self._ensure_not_removed(lifetime)
        self._begin_pending(target.name, source or self._last_observed_mode)
        try:
            if target == Mode.REAL_FEEL or source == Mode.REAL_FEEL.name:
                return await self._send_real_feel(exit_mode=target == Mode.DEHUMIDIFY)
            return await self.client.set_mode(target)
        finally:
            if self._lifetime_generation == lifetime and not self._removed:
                self._finish_pending()

    async def _send_real_feel(self, *, exit_mode: bool) -> dict:
        """Send a single non-redirecting request with a safe error on failure."""
        try:
            if exit_mode:
                return await self.client.request_exit_real_feel()
            return await self.client.request_enter_real_feel()
        except InvalidCredentialsError:
            raise
        except Exception:  # noqa: BLE001 - avoid cloud response details
            what = "exit request" if exit_mode else "request"
            key = (
                "real_feel_write_failed"
                if exit_mode
                else "real_feel_enter_write_failed"
            )
            raise HomeAssistantError(
                f"The Real Feel {what} failed; the device may have received it. "
                "Check the De'Longhi app before trying again.",
                translation_domain=DOMAIN,
                translation_key=key,
            ) from None

    def _clear_pending(self) -> None:
        """Clear a mode request and prevent an older timeout from affecting a later one."""
        self._pending_generation += 1
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        if self._pending_refresh_timer is not None:
            self._pending_refresh_timer.cancel()
            self._pending_refresh_timer = None
        self._pending_refresh_remaining = 0
        self._pending_mode = None
        self._pending_source = None
        self._pending_earlier_modes.clear()
        self._pending_deadline = None

    def _begin_pending(self, mode: str, source: str | None) -> None:
        """Mark an attempted write unknown until the cloud catches up."""
        earlier_modes = set(self._pending_earlier_modes)
        if self._pending_mode is not None:
            earlier_modes.add(self._pending_mode)
        self._clear_pending()
        self._pending_mode = mode
        self._pending_source = source
        self._pending_earlier_modes = earlier_modes
        self._attr_mode = None
        self.async_write_ha_state()

    def _finish_pending(self) -> None:
        """Bound a pending request after its write completes or fails."""
        if self._removed:
            return
        self._pending_deadline = self.hass.loop.time() + _PENDING_SECONDS
        generation = self._pending_generation
        self._pending_timer = self.hass.loop.call_later(
            _PENDING_SECONDS, self._pending_expired, generation
        )
        if (
            self._pending_mode == Mode.REAL_FEEL.name
            and not self.client.invalid_credentials
        ):
            self._pending_refresh_remaining = _PENDING_REFRESH_LIMIT
            self._pending_refresh_timer = self.hass.loop.call_later(
                _PENDING_REFRESH_INTERVAL, self._pending_refresh_tick, generation
            )
        self._schedule_refresh()

    def _pending_refresh_tick(self, generation: int) -> None:
        """Request a bounded follow-up observation while activation is pending."""
        if generation != self._pending_generation:
            return
        self._pending_refresh_timer = None
        if (
            self._removed
            or self.client.invalid_credentials
            or self._pending_mode != Mode.REAL_FEEL.name
            or self._pending_deadline is None
            or self.hass.loop.time() >= self._pending_deadline
        ):
            return
        self._pending_refresh_remaining -= 1
        self._schedule_refresh()
        if self._pending_refresh_remaining:
            self._pending_refresh_timer = self.hass.loop.call_later(
                _PENDING_REFRESH_INTERVAL, self._pending_refresh_tick, generation
            )

    def _pending_expired(self, generation: int) -> None:
        """After the bounded wait, request a new snapshot rather than use the cache."""
        if self._removed or generation != self._pending_generation:
            return
        self._pending_timer = None
        if self._pending_refresh_timer is not None:
            self._pending_refresh_timer.cancel()
            self._pending_refresh_timer = None
        self._pending_refresh_remaining = 0
        self._poll_generation += 1
        self._completed_poll_generation = self._poll_generation
        self.client.invalidate_properties()
        self._schedule_refresh()

    def _expire_due_pending(self) -> None:
        """Start the expiration refresh if the loop has not run its timer yet."""
        if (
            self._pending_deadline is not None
            and self.hass.loop.time() >= self._pending_deadline
            and self._pending_timer is not None
        ):
            self._pending_timer.cancel()
            self._pending_expired(self._pending_generation)

    def _mode_with_pending(self, observed: str | None) -> str | None:
        """Suppress a conflicting echo while our request may still be syncing."""
        if self._pending_mode is None:
            return observed
        if self._pending_deadline is None:
            return None
        if self.hass.loop.time() >= self._pending_deadline:
            if self._pending_timer is not None:
                self._expire_due_pending()
                return None
            self._clear_pending()
            return observed
        distinct_ordinary_mode = (
            self._pending_source is not None
            and observed in (Mode.DRY_CLOTHES.name, Mode.PURIFIER.name)
            and observed != self._pending_source
            and observed != self._pending_mode
            and observed not in self._pending_earlier_modes
        )
        if observed == self._pending_mode or distinct_ordinary_mode:
            self._clear_pending()
            return observed
        return None

    async def async_set_humidity(self, humidity: int) -> None:
        """Set new target humidity."""
        _LOGGER.debug("Setting humidity %s mode to %s", self._attr_unique_id, humidity)
        await self.client.humidifier_commands.run(
            lambda: self.client.set_humidity(humidity)
        )
