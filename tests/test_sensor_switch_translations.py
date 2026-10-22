"""Exercise localized sensor and switch names through Home Assistant."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api import sensor, switch
from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

# Keys are derived from the original English type_name, which also defines unique IDs.
SENSOR_NAMES = {
    "current_humidity": ("Current Humidity", "Aktuelle Luftfeuchtigkeit"),
    "target_humidity": ("Target Humidity", "Soll-Luftfeuchtigkeit"),
    "current_speed": ("Current Speed", "Aktuelle Geschwindigkeit"),
    "filter_status": ("Filter Status", "Filterstatus"),
    "room_temperature": ("Room Temperature", "Raumtemperatur"),
    "heat_exchanger_temperature": (
        "Heat Exchanger Temperature",
        "Wärmetauschertemperatur",
    ),
    "device_mode": ("Device Mode", "Gerätemodus"),
    "device_status": ("Device Status", "Gerätestatus"),
    "eco_mode": ("Eco Mode", "Eco-Modus"),
    "swing_mode": ("Swing Mode", "Schwenkmodus"),
    "filter_change_alarm": ("Filter Change Alarm", "Filterwechselalarm"),
    "filter_life": ("Filter Life", "Filterlebensdauer"),
}
SWITCH_NAMES = {key: SENSOR_NAMES[key] for key in ("eco_mode", "swing_mode")}
ENTITY_NAMES = {"sensor": SENSOR_NAMES, "switch": SWITCH_NAMES}


class LegacySensor(sensor.GenericSensor):
    """Reproduce the prior hardcoded sensor name for upgrade tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_translation_key = None
        self._attr_name = args[3]


class LegacySwitch(switch.GenericOffOnSwitchSensor):
    """Reproduce the prior hardcoded switch name for upgrade tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attr_translation_key = None
        self._attr_name = args[3]


@pytest.fixture
async def cloud(hass):
    """Provide a mutable, synthetic appliance without cloud access."""
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
        "set_eco": 1,
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
        patch.object(APIClient, "get_request", AsyncMock(side_effect=get_properties)),
        patch.object(APIClient, "post_request", AsyncMock(return_value={})),
    ):
        yield entry, values
        if "automation" in hass.data:
            await hass.data["automation"]._async_reset()
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def unique_id(domain, key):
    """Generate the IDs used before localization from original English names."""
    return f"{DOMAIN}_test-dsn_{key}_{domain}"


def registered(registry, domain, key):
    """Resolve a sensor or switch by its original unique ID."""
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id(domain, key))
    assert entity_id
    return registry.async_get(entity_id)


def observed_states(hass, registry):
    """Capture states, units, classes, options and statistics metadata."""
    fields = ("device_class", "state_class", "unit_of_measurement", "options")
    states = {}
    for domain, names in ENTITY_NAMES.items():
        for key in names:
            entry = registered(registry, domain, key)
            state = hass.states.get(entry.entity_id)
            assert state is not None
            states[(domain, key)] = (
                state.state,
                {field: state.attributes.get(field) for field in fields},
            )
    return states


def assert_names(hass, registry, translations, language):
    """Check all 14 registered entries, identities and default friendly names."""
    index = 1 if language == "de" else 0
    for domain, names in ENTITY_NAMES.items():
        for key, values in names.items():
            entry = registered(registry, domain, key)
            assert entry.unique_id == unique_id(domain, key)
            assert entry.translation_key == key
            assert entry.original_name == values[index]
            assert hass.states.get(entry.entity_id).attributes["friendly_name"] == (
                f"Test Dehumidifier {values[index]}"
            )
            assert (
                translations[f"component.{DOMAIN}.entity.{domain}.{key}.name"]
                == values[index]
            )


@pytest.mark.parametrize("language", ["en", "de", "fr"])
async def test_new_sensor_and_switch_names(hass, cloud, language):
    """Fresh setup loads all names and English fallback using the HA loader."""
    hass.config.language = language
    entry, _ = cloud
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entries) == 16
    translations = await async_get_translations(hass, language, "entity", [DOMAIN])
    assert_names(hass, registry, translations, language)


def assert_before_names(registry):
    """Confirm every original entity has its hardcoded English name."""
    for domain, names in ENTITY_NAMES.items():
        for key, (english, _) in names.items():
            entry = registered(registry, domain, key)
            assert entry.original_name == english
            assert entry.translation_key is None


def assert_upgraded_names(registry, language):
    """Each upgraded entity receives its translated default name."""
    for domain, names in ENTITY_NAMES.items():
        for key, values in names.items():
            entry = registered(registry, domain, key)
            assert entry.original_name == values[language == "de"]
            assert entry.translation_key == key


def snapshot_ids(registry, entry):
    """Record registry UUIDs and entity IDs of the whole integration."""
    return {
        item.unique_id: (item.id, item.entity_id)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def assert_overrides(hass, registry, area, language):
    """User-owned names, icon, area and alias survive updating."""
    unit = registered(registry, "sensor", "current_humidity")
    eco = registered(registry, "switch", "eco_mode")
    assert unit.entity_id == "sensor.my_existing_humidity"
    assert (unit.name, unit.icon, unit.area_id, unit.aliases) == (
        "My humidity",
        "mdi:star",
        area.id,
        {"My alias"},
    )
    assert unit.original_name == SENSOR_NAMES["current_humidity"][language == "de"]
    assert hass.states.get(unit.entity_id).attributes["friendly_name"] == "My humidity"
    assert eco.entity_id == "switch.my_existing_eco"
    assert eco.name == "My eco"
    assert eco.original_name == SWITCH_NAMES["eco_mode"][language == "de"]
    assert hass.states.get(eco.entity_id).attributes["friendly_name"] == "My eco"


def assert_disabled(registry, disabled):
    """Previously disabled entries remain disabled."""
    for domain, key in disabled:
        entry = registered(registry, domain, key)
        assert entry.disabled_by == er.RegistryEntryDisabler.USER


def assert_enabled(hass, registry, disabled, original_ids):
    """Re-enabling retains each ID and starts the same entity."""
    for domain, key in disabled:
        entry = registered(registry, domain, key)
        assert (entry.id, entry.entity_id) == original_ids[entry.unique_id]
        assert entry.disabled_by is None
        assert entry.translation_key == key
        assert hass.states.get(entry.entity_id) is not None


async def setup_automation(hass, eco_id):
    """Observe an automation bound to the existing switch entity ID."""
    events = []
    hass.bus.async_listen(
        "localized_sensor_switch_test", lambda event: events.append(event)
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "localized_sensor_switch_test",
                    "alias": "Existing eco automation",
                    "trigger": [
                        {"platform": "state", "entity_id": eco_id, "to": "off"}
                    ],
                    "action": [{"event": "localized_sensor_switch_test"}],
                }
            ]
        },
    )
    await hass.async_block_till_done()
    return events


@pytest.mark.parametrize("language", ["en", "de"])
async def test_upgrade_preserves_all_entities_and_automation(hass, cloud, language):
    """Upgrade existing registry data, including disabled entities and an automation."""
    hass.config.language = language
    entry, _ = cloud
    with (
        patch.object(sensor, "GenericSensor", LegacySensor),
        patch.object(switch, "GenericOffOnSwitchSensor", LegacySwitch),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert_before_names(registry)
        before_states = observed_states(hass, registry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    area = ar.async_get(hass).async_create("Test room")
    humidity = registered(registry, "sensor", "current_humidity")
    registry.async_update_entity(
        humidity.entity_id,
        new_entity_id="sensor.my_existing_humidity",
        name="My humidity",
        icon="mdi:star",
        area_id=area.id,
        aliases={"My alias"},
    )
    eco = registered(registry, "switch", "eco_mode")
    registry.async_update_entity(
        eco.entity_id, new_entity_id="switch.my_existing_eco", name="My eco"
    )
    disabled = [("sensor", "filter_status"), ("switch", "swing_mode")]
    for domain, key in disabled:
        item = registered(registry, domain, key)
        registry.async_update_entity(
            item.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    await hass.async_block_till_done()
    original_ids = snapshot_ids(registry, entry)
    assert len(original_ids) == 16
    events = await setup_automation(hass, "switch.my_existing_eco")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert snapshot_ids(registry, entry) == original_ids
    assert_overrides(hass, registry, area, language)
    assert_disabled(registry, disabled)
    for domain, key in disabled:
        item = registered(registry, domain, key)
        registry.async_update_entity(item.entity_id, disabled_by=None)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert_enabled(hass, registry, disabled, original_ids)
    assert_upgraded_names(registry, language)
    assert observed_states(hass, registry) == before_states

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.my_existing_eco"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("switch.my_existing_eco").state == "off"
    assert len(events) == 1
