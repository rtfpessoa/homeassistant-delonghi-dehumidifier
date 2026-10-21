"""Test primary entity categories and existing registry customizations."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

PRIMARY_ENTITIES = (
    ("humidifier", "dehumidifier", EntityCategory.CONFIG),
    ("sensor", "current_humidity_sensor", EntityCategory.DIAGNOSTIC),
    ("sensor", "room_temperature_sensor", EntityCategory.DIAGNOSTIC),
)


@pytest.fixture
async def setup_entities(hass, request):
    """Set up the integration with fresh or pre-existing registry entries."""
    scenario = getattr(request, "param", "existing")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = {}

    if scenario != "fresh":
        area = ar.async_get(hass).async_create("Test room")
        category = cr.async_get(hass).async_create(name="My category", scope="entity")
        label = lr.async_get(hass).async_create(name="My label")
        for entity_domain, suffix, old_category in PRIMARY_ENTITIES:
            unique_id = f"{DOMAIN}_test-dsn_{suffix}"
            old = registry.async_get_or_create(
                entity_domain,
                DOMAIN,
                unique_id,
                config_entry=entry,
                entity_category=old_category,
            )
            old = registry.async_update_entity(
                old.entity_id,
                new_entity_id=f"{entity_domain}.my_{suffix}",
                name=f"My {suffix}",
                icon="mdi:star",
                area_id=area.id,
                aliases={"Custom alias"},
                categories={"entity": category.category_id},
                labels={label.label_id},
                hidden_by=er.RegistryEntryHider.USER,
                disabled_by=(
                    er.RegistryEntryDisabler.USER
                    if scenario == "disabled" and suffix == "room_temperature_sensor"
                    else None
                ),
            )
            if suffix == "room_temperature_sensor":
                registry.async_update_entity_options(
                    old.entity_id,
                    "sensor",
                    {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
                )
            existing[unique_id] = registry.async_get(old.entity_id)

    values = {
        "appliance_model": "DDSX220WFA",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 1,
        "device_mode": 1,
        "device_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "filter_status": 1,
        "heat_exchanger_temp": 220,
        "room_temp": 210,
        "swing": 0,
        "set_eco": 0,
    }
    properties = [{"product_name": "Test"}] + [
        {"name": name, "value": value} for name, value in values.items()
    ]
    with patch.multiple(
        APIClient,
        get_first_device=AsyncMock(return_value="test-dsn"),
        get_properties=AsyncMock(return_value=properties),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield registry, entry, existing
        assert await hass.config_entries.async_unload(entry.entry_id)


def primary_entries(registry):
    """Look up primary entities by their integration-stable unique IDs."""
    result = {}
    for domain, suffix, _ in PRIMARY_ENTITIES:
        unique_id = f"{DOMAIN}_test-dsn_{suffix}"
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None
        result[unique_id] = registry.async_get(entity_id)
    return result


@pytest.mark.parametrize(
    "setup_entities", ["fresh", "existing", "disabled"], indirect=True
)
async def test_primary_entities_have_no_category(setup_entities):
    """Home Assistant updates primary categories for new and existing entities."""
    registry, _, _ = setup_entities
    current = primary_entries(registry)
    assert {key: entity.entity_category for key, entity in current.items()} == {
        f"{DOMAIN}_test-dsn_{suffix}": None for _, suffix, _ in PRIMARY_ENTITIES
    }


async def test_existing_primary_entities_keep_registry_ids(setup_entities):
    """Keep unique IDs, entity IDs and registry IDs after setup."""
    registry, _, previous = setup_entities
    current = primary_entries(registry)
    fields = ("id", "entity_id", "unique_id")
    assert {
        key: tuple(getattr(entity, field) for field in fields)
        for key, entity in current.items()
    } == {
        key: tuple(getattr(entity, field) for field in fields)
        for key, entity in previous.items()
    }


async def test_existing_primary_entities_keep_user_settings(setup_entities):
    """Preserve the settings that the user can change in the entity registry."""
    registry, _, previous = setup_entities
    current = primary_entries(registry)
    fields = (
        "name",
        "icon",
        "area_id",
        "aliases",
        "categories",
        "labels",
        "hidden_by",
        "disabled_by",
    )
    assert {
        key: tuple(getattr(entity, field) for field in fields)
        for key, entity in current.items()
    } == {
        key: tuple(getattr(entity, field) for field in fields)
        for key, entity in previous.items()
    }


async def test_existing_room_temperature_keeps_user_unit(hass, setup_entities):
    """A user-selected Fahrenheit unit continues to control the displayed state."""
    registry, _, previous = setup_entities
    unique_id = f"{DOMAIN}_test-dsn_room_temperature_sensor"
    entity = primary_entries(registry)[unique_id]
    assert (
        entity.options["sensor"]["unit_of_measurement"]
        == previous[unique_id].options["sensor"]["unit_of_measurement"]
    )
    state = hass.states.get(entity.entity_id)
    assert state.attributes["unit_of_measurement"] == UnitOfTemperature.FAHRENHEIT
    assert float(state.state) == pytest.approx(69.8)


@pytest.mark.parametrize("setup_entities", ["disabled"], indirect=True)
async def test_disabled_entity_remains_disabled(hass, setup_entities):
    """Do not enable a previously disabled entity when updating its category."""
    registry, _, _ = setup_entities
    entity = primary_entries(registry)[f"{DOMAIN}_test-dsn_room_temperature_sensor"]
    assert entity.disabled_by == er.RegistryEntryDisabler.USER
    assert hass.states.get(entity.entity_id) is None


async def test_other_sensors_remain_diagnostic(setup_entities):
    """Leave categories of all other sensors unchanged."""
    registry, entry, _ = setup_entities
    primary_ids = set(primary_entries(registry))
    other_sensors = [
        entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.domain == "sensor" and entity.unique_id not in primary_ids
    ]
    assert len(other_sensors) == 10
    assert all(
        entity.entity_category == EntityCategory.DIAGNOSTIC for entity in other_sensors
    )
