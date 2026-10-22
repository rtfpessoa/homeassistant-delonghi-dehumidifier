"""Explicit Real Feel exit request for the tested DDSX220 firmware."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import APIClient, InvalidCredentialsError
from .const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
)
from .entity import AuthAvailabilityEntity
from .utils import fetch_device_info

_DEVICE_REMOVED = (
    "The device is no longer available. Reload the integration and try again."
)
_UNSUPPORTED = (
    "This request requires a DDSX220 with firmware 1.0. "
    "Check the device information and reload the integration."
)
_NOT_READY = (
    "The cloud data is not ready for this request. Select Real Feel in the "
    "De'Longhi app, wait for it to sync, and try again."
)


def _validation_error(key: str, message: str) -> ServiceValidationError:
    """Provide a fallback message and translation metadata to Home Assistant."""
    return ServiceValidationError(
        message, translation_domain=DOMAIN, translation_key=key
    )


def _value(properties: list[dict], name: str):
    """Read exactly one property, rejecting missing and duplicate values."""
    if type(properties) is not list or any(
        type(item) is not dict for item in properties
    ):
        return None
    matches = [item.get("value") for item in properties if item.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def _supported(properties: list[dict]) -> bool:
    """Recognize only the model and firmware tested on the appliance."""
    model = _value(properties, "appliance_model")
    firmware = _value(properties, "firmware_version")
    return (
        type(model) is str
        and model == "DDSX220"
        and (
            (type(firmware) is str and firmware == "1.0")
            or (type(firmware) is float and firmware == 1.0)
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry[APIClient],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the button only when the cached identity matches the tested device."""
    client = config_entry.runtime_data
    if not _supported(await client.get_properties()):
        return
    device_info = await fetch_device_info(client)
    device_dsn = await client.get_first_device()
    async_add_entities([RealFeelExitButton(device_dsn, device_info, client)])


class RealFeelExitButton(AuthAvailabilityEntity, ButtonEntity):
    """Request one tested command without asserting the resulting mode."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_translation_key = "request_exit_real_feel"

    def __init__(
        self, device_dsn: str, device_info: DeviceInfo, client: APIClient
    ) -> None:
        """Set a stable identity and fixed command for this button."""
        self.client = client
        self._removed = False
        self._attr_device_info = device_info
        self._attr_unique_id = f"{DOMAIN}_{device_dsn}_{self._attr_translation_key}"

    async def async_added_to_hass(self) -> None:
        """Track entity removal while a request is awaiting its turn."""
        await super().async_added_to_hass()
        self.async_on_remove(self._mark_removed)

    def _mark_removed(self) -> None:
        self._removed = True

    async def _guard(self) -> None:
        """Validate a snapshot fetched after this request acquired the lock."""
        if self._removed:
            raise _validation_error("device_removed", _DEVICE_REMOVED)
        self.client.invalidate_properties()
        try:
            properties = await self.client.get_properties()
        except InvalidCredentialsError:
            raise
        except Exception:  # noqa: BLE001 - don't expose cloud response details
            raise HomeAssistantError(
                "Unable to check the current device data. Check the connection and try again.",
                translation_domain=DOMAIN,
                translation_key="real_feel_read_failed",
            ) from None
        if self._removed:
            raise _validation_error("device_removed", _DEVICE_REMOVED)
        if not _supported(properties):
            raise _validation_error("real_feel_unsupported", _UNSUPPORTED)
        mode = _value(properties, "device_mode")
        if type(mode) is not int or mode != 1:
            raise _validation_error("real_feel_not_ready", _NOT_READY)
        previous = _value(properties, "activate_realfeel")
        if type(previous) is not str or previous != REAL_FEEL_ACTIVATION_PAYLOAD:
            raise _validation_error("real_feel_not_ready", _NOT_READY)

    async def _send(self) -> dict:
        """Send once, keeping transport details out of the action error."""
        if self._removed:
            raise _validation_error("device_removed", _DEVICE_REMOVED)
        try:
            return await self.client.request_exit_real_feel()
        except InvalidCredentialsError:
            raise
        except Exception:  # noqa: BLE001 - don't expose cloud response details
            raise HomeAssistantError(
                "The Real Feel exit request failed; the device may have received it. "
                "Check the De'Longhi app before trying again.",
                translation_domain=DOMAIN,
                translation_key="real_feel_write_failed",
            ) from None

    async def async_press(self) -> None:
        """Validate and send the request under the humidifier command lock."""
        await self.client.humidifier_commands.run(
            self._send, guard=self._guard, refresh_after_success=True
        )
