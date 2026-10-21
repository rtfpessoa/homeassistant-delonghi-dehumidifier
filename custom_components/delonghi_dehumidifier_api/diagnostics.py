"""Privacy-conscious diagnostics for the DeLonghi dehumidifier."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant

from . import DeLonghiDehumidifierConfigEntry

_PROPERTY_NAMES = ("alarm_state", "set_eco", "swing")


def _small_integer(value: Any) -> int | None:
    """Keep only 32-bit integer values, excluding booleans and text."""
    if type(value) is int and -(2**31) <= value < 2**31:
        return value
    return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeLonghiDehumidifierConfigEntry
) -> dict[str, Any]:
    """Return only selected values from the most recent device property cache."""
    client = entry.runtime_data
    cached_properties = client.device_properties
    if not isinstance(cached_properties, list):
        return {"status": "unavailable", "cache_age_seconds": None, "properties": {}}

    timestamp = client.device_properties_timestamp
    age = None
    now = time.time()
    if type(timestamp) in (int, float) and 0 < timestamp <= now:
        age = int(now - timestamp)

    properties: dict[str, dict[str, Any]] = {
        name: {"present": False, "value": None, "ack_enabled": None, "ack_status": None}
        for name in _PROPERTY_NAMES
    }
    for item in cached_properties:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in properties:
            continue
        ack_enabled = item.get("ack_enabled")
        properties[name] = {
            "present": True,
            "value": _small_integer(item.get("value")),
            "ack_enabled": ack_enabled if type(ack_enabled) is bool else None,
            "ack_status": _small_integer(item.get("ack_status")),
        }

    return {"status": "available", "cache_age_seconds": age, "properties": properties}
