"""Privacy-conscious diagnostics for the DeLonghi dehumidifier."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant

from . import DeLonghiDehumidifierConfigEntry
from .const import REAL_FEEL_ACTIVATION_PAYLOAD

_CANDIDATE_DEACTIVATION_PAYLOAD = "AAIDChIXHEY8Mig="
_PROPERTY_NAMES = (
    "alarm_state",
    "set_eco",
    "swing",
    "device_mode",
    "activate_realfeel",
)


def _small_integer(value: Any) -> int | None:
    """Keep only 32-bit integer values, excluding booleans and text."""
    if type(value) is int and -(2**31) <= value < 2**31:
        return value
    return None


def _byte_integer(value: Any) -> int | None:
    """Keep only byte-sized integers, excluding booleans and other types."""
    if type(value) is int and 0 <= value <= 255:
        return value
    return None


def _cache_age(timestamp: Any) -> int | None:
    """Return the time since a valid wall-clock completion timestamp."""
    now = time.time()
    if type(timestamp) in (int, float) and 0 < timestamp <= now:
        return int(now - timestamp)
    return None


def _request_age(started: Any) -> int | None:
    """Return the time since a valid monotonic request start."""
    now = time.monotonic()
    if type(started) in (int, float) and 0 <= started <= now:
        return int(now - started)
    return None


def _safe_property_value(name: str, value: Any) -> int | None:
    """Limit values according to the diagnostic property's purpose."""
    if name == "activate_realfeel":
        return None
    if name == "device_mode":
        return _byte_integer(value)
    return _small_integer(value)


def _matches_command(value: Any, expected: str) -> bool | None:
    """Compare strings without reporting their contents."""
    return value == expected if type(value) is str else None


def _export_property(name: str, item: dict[str, Any]) -> dict[str, Any]:
    """Export only the explicitly allowed information for one property."""
    value = item.get("value")
    ack_enabled = item.get("ack_enabled")
    ack_status = item.get("ack_status")
    if name in ("device_mode", "activate_realfeel"):
        ack_status = _byte_integer(ack_status)
    else:
        ack_status = _small_integer(ack_status)
    result = {
        "present": True,
        "value": _safe_property_value(name, value),
        "ack_enabled": ack_enabled if type(ack_enabled) is bool else None,
        "ack_status": ack_status,
    }
    if name == "device_mode":
        result["value_out_of_range"] = (
            not 0 <= value <= 255 if type(value) is int else None
        )
    if name == "activate_realfeel":
        result["matches_activation_command"] = _matches_command(
            value, REAL_FEEL_ACTIVATION_PAYLOAD
        )
        result["matches_candidate_deactivation_command"] = _matches_command(
            value, _CANDIDATE_DEACTIVATION_PAYLOAD
        )
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> dict[str, Any]:
    """Return only selected values from the most recent device property cache."""
    client = entry.runtime_data
    cached_properties = client.device_properties
    if not isinstance(cached_properties, list):
        return {
            "status": "unavailable",
            "cache_age_seconds": None,
            "request_age_seconds": None,
            "properties": {},
        }

    properties: dict[str, dict[str, Any]] = {
        name: {"present": False, "value": None, "ack_enabled": None, "ack_status": None}
        for name in _PROPERTY_NAMES
    }
    properties["device_mode"]["value_out_of_range"] = None
    properties["activate_realfeel"]["matches_activation_command"] = None
    properties["activate_realfeel"]["matches_candidate_deactivation_command"] = None
    for item in cached_properties:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in properties:
            continue
        properties[name] = _export_property(name, item)

    return {
        "status": "available",
        "cache_age_seconds": _cache_age(client.device_properties_timestamp),
        "request_age_seconds": _request_age(
            getattr(client, "device_properties_request_started", None)
        ),
        "properties": properties,
    }
