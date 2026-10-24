"""Create and check device-bound entries in Home Assistant tests."""

import json
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import (
    CONF_DSN,
    JOURNAL_KEY,
    device_unique_id,
)


class BoundMockConfigEntry(MockConfigEntry):
    """Represent an entry whose earlier selection has a matching durable claim."""

    def add_to_hass(self, hass):
        super().add_to_hass(hass)
        path = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            json.loads(path.read_text())
            if path.exists()
            else {"version": 1, "claims": {}}
        )
        data["claims"].setdefault(
            self.unique_id, {"owner": self.entry_id, "losers": []}
        )
        path.write_text(json.dumps(data))
        hass.data[JOURNAL_KEY] = data["claims"]


def bound_entry(*, dsn="test-dsn", **kwargs):
    kwargs["data"] = {**kwargs.get("data", {}), CONF_DSN: dsn}
    kwargs["unique_id"] = device_unique_id(dsn)
    kwargs["version"] = 2
    return BoundMockConfigEntry(**kwargs)


async def assert_legacy_binding_pending(hass, entry):
    """Verify a legacy entry awaits device confirmation without binding."""
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert CONF_DSN not in entry.data


def assert_pinned_identity(entry, dsn):
    """Verify a loaded entry and its client agree on the selected device."""
    assert entry.data[CONF_DSN] == dsn
    assert entry.unique_id == device_unique_id(dsn)
    assert entry.runtime_data.device_dsn == dsn


def assert_device_associations(registry, entry_id, expected_device_ids):
    """Verify a config entry retains its existing device associations."""
    assert {
        item.id for item in dr.async_entries_for_config_entry(registry, entry_id)
    } == expected_device_ids
