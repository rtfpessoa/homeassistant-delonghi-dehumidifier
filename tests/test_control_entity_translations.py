"""Check translated control entities and existing registry customizations."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.delonghi_dehumidifier_api import binary_sensor, humidifier
from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests.identity_helpers import bound_entry as MockConfigEntry

UNIT_UNIQUE_ID = f"{DOMAIN}_test-dsn_dehumidifier"
ALARM_UNIQUE_ID = f"{DOMAIN}_test-dsn_device_alarm_binary_sensor"


class LegacyHumidifier(humidifier.DehumidifierEntity):
    """Reproduce the previous release's hardcoded humidifier entity name."""

    _attr_translation_key = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_name = "Unit"


class LegacyAlarm(binary_sensor.DeviceAlarm):
    """Reproduce the previous release's hardcoded alarm entity name."""

    _attr_translation_key = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_name = "Device alarm"


@pytest.fixture
async def cloud(hass):
    """Supply mutable properties without contacting the De'Longhi API."""
    values = {
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 1,
        "alarm_state": 3,
    }

    async def get_properties(_path):
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in values.items()
        ]

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.invalid",
            CONF_PASSWORD: "test-password",
            CONF_LANGUAGE: "pt",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["test-dsn"])),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=get_properties)),
        patch.object(APIClient, "post_request", AsyncMock(return_value={})),
    ):
        yield entry, values
        if "automation" in hass.data:
            await hass.data["automation"]._async_reset()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def entity(registry, domain, unique_id):
    """Find a registered entity by its unchanged unique ID."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id
    return registry.async_get(entity_id)


@pytest.mark.parametrize(
    ("language", "unit_name", "alarm_name"),
    [
        ("en", "Unit", "Device alarm"),
        ("de", "Gerät", "Gerätealarm"),
        ("fr", "Unit", "Device alarm"),
    ],
)
async def test_new_entities_use_server_language_and_keep_states(
    hass, cloud, language, unit_name, alarm_name
):
    """Fresh setup loads names in the server language with English fallback."""
    hass.config.language = language
    entry, _ = cloud
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    unit = entity(registry, "humidifier", UNIT_UNIQUE_ID)
    alarm = entity(registry, "binary_sensor", ALARM_UNIQUE_ID)
    assert unit.original_name == unit_name
    assert alarm.original_name == alarm_name
    assert unit.translation_key == "unit"
    assert alarm.translation_key == "device_alarm"
    assert hass.states.get(unit.entity_id).attributes["friendly_name"] == (
        f"Test Dehumidifier {unit_name}"
    )
    assert hass.states.get(alarm.entity_id).attributes["friendly_name"] == (
        f"Test Dehumidifier {alarm_name}"
    )
    localized = await async_get_translations(hass, language, "entity", [DOMAIN])
    assert localized[f"component.{DOMAIN}.entity.humidifier.unit.name"] == unit_name
    assert localized[f"component.{DOMAIN}.entity.binary_sensor.device_alarm.name"] == (
        alarm_name
    )


async def test_fresh_controls_keep_states(hass, cloud):
    """Localized names do not alter humidifier or alarm state data."""
    entry, _ = cloud
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    unit = entity(registry, "humidifier", UNIT_UNIQUE_ID)
    alarm = entity(registry, "binary_sensor", ALARM_UNIQUE_ID)
    unit_state = hass.states.get(unit.entity_id)
    alarm_state = hass.states.get(alarm.entity_id)
    assert unit_state.state == "on"
    assert unit_state.attributes["device_class"] == "dehumidifier"
    assert unit_state.attributes["mode"] == "DEHUMIDIFY"
    assert unit_state.attributes["available_modes"] == [
        "DEHUMIDIFY",
        "DRY_CLOTHES",
        "PURIFIER",
    ]
    assert unit_state.attributes["humidity"] == 55
    assert unit_state.attributes["current_humidity"] == 50
    assert alarm_state.state == "on"
    assert alarm_state.attributes["device_class"] == "problem"


def assert_legacy_names(unit, alarm):
    """Confirm the pre-upgrade entities use their original hardcoded names."""
    assert unit.original_name == "Unit"
    assert alarm.original_name == "Device alarm"
    assert unit.translation_key is None and alarm.translation_key is None


def assert_custom_unit(hass, unit, area, language):
    """Check the localized default and every user-owned humidifier setting."""
    assert unit.translation_key == "unit"
    assert unit.original_name == ("Gerät" if language == "de" else "Unit")
    assert unit.name == "My custom humidifier"
    assert unit.icon == "mdi:star"
    assert unit.area_id == area.id
    assert unit.aliases == {"My alias"}
    assert hass.states.get(unit.entity_id).attributes["friendly_name"] == (
        "My custom humidifier"
    )


def assert_disabled_alarm(hass, alarm):
    """The alarm remains disabled until the user re-enables it."""
    assert alarm.name == "My custom alarm"
    assert alarm.disabled_by == er.RegistryEntryDisabler.USER
    state = hass.states.get(alarm.entity_id)
    assert state is None or (
        state.state == "unavailable" and state.attributes.get("restored")
    )


def assert_enabled_alarm(hass, alarm, before, language):
    """Re-enable the existing alarm without losing identity or custom name."""
    assert (alarm.id, alarm.entity_id) == before[ALARM_UNIQUE_ID]
    assert alarm.translation_key == "device_alarm"
    assert alarm.original_name == (
        "Gerätealarm" if language == "de" else "Device alarm"
    )
    assert alarm.name == "My custom alarm"
    assert hass.states.get(alarm.entity_id).attributes["friendly_name"] == (
        "My custom alarm"
    )


@pytest.mark.parametrize("language", ["en", "de"])
async def test_upgrade_preserves_registry_and_existing_automation(
    hass, cloud, language
):
    """Reload from legacy naming preserves IDs, user settings, and state triggers."""
    hass.config.language = language
    entry, values = cloud
    with (
        patch.object(humidifier, "DehumidifierEntity", LegacyHumidifier),
        patch.object(binary_sensor, "DeviceAlarm", LegacyAlarm),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    unit = entity(registry, "humidifier", UNIT_UNIQUE_ID)
    alarm = entity(registry, "binary_sensor", ALARM_UNIQUE_ID)
    assert_legacy_names(unit, alarm)

    area = ar.async_get(hass).async_create("Test room")
    unit = registry.async_update_entity(
        unit.entity_id,
        new_entity_id="humidifier.existing_custom_id",
        name="My custom humidifier",
        icon="mdi:star",
        area_id=area.id,
        aliases={"My alias"},
    )
    alarm = registry.async_update_entity(
        alarm.entity_id,
        new_entity_id="binary_sensor.existing_custom_id",
        name="My custom alarm",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    before = {
        item.unique_id: (item.id, item.entity_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert len(before) == 16
    events = []
    hass.bus.async_listen("translated_control_test", lambda event: events.append(event))
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "translated_control_test",
                    "alias": "Existing humidifier automation",
                    "trigger": [
                        {"platform": "state", "entity_id": unit.entity_id, "to": "off"}
                    ],
                    "action": [{"event": "translated_control_test"}],
                }
            ]
        },
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    after = {
        item.unique_id: (item.id, item.entity_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    updated_unit = entity(registry, "humidifier", UNIT_UNIQUE_ID)
    updated_alarm = entity(registry, "binary_sensor", ALARM_UNIQUE_ID)
    assert_custom_unit(hass, updated_unit, area, language)
    assert_disabled_alarm(hass, updated_alarm)

    registry.async_update_entity(updated_alarm.entity_id, disabled_by=None)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    enabled_alarm = entity(registry, "binary_sensor", ALARM_UNIQUE_ID)
    assert_enabled_alarm(hass, enabled_alarm, before, language)

    values["device_status"] = 2
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": updated_unit.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(updated_unit.entity_id).state == "off"
    assert len(events) == 1
