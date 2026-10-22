"""Verify the retired Real Feel button is removed without affecting other entities."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
)
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from tests import real_feel_helpers, test_real_feel_mode

cloud = real_feel_helpers.cloud


async def test_fresh_install_does_not_create_a_button(hass, cloud):
    await real_feel_helpers.setup(hass, cloud)
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{DOMAIN}_test-dsn_request_exit_real_feel"
        )
        is None
    )
    assert hass.states.get(test_real_feel_mode.humidifier_id(hass)) is not None
    cloud[4].assert_not_awaited()


@pytest.mark.parametrize(
    "choice", ["default", "hidden", "integration_hidden", "disabled", "renamed"]
)
async def test_upgrade_removes_old_button_with_user_choices(hass, cloud, choice):
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    changes = {
        "hidden": {"hidden_by": er.RegistryEntryHider.USER},
        "integration_hidden": {"hidden_by": er.RegistryEntryHider.INTEGRATION},
        "disabled": {"disabled_by": er.RegistryEntryDisabler.USER},
        "renamed": {"new_entity_id": "button.my_custom_exit", "name": "My Exit"},
    }
    if choice in changes:
        old = registry.async_update_entity(old.entity_id, **changes[choice])
    await real_feel_helpers.setup(hass, cloud)
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(old.entity_id) is None


async def test_cleanup_is_entry_and_entity_specific(hass, cloud):
    registry = er.async_get(hass)
    other_entry = MockConfigEntry(domain=DOMAIN)
    other_entry.add_to_hass(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_old-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    other = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_other-dsn_request_exit_real_feel",
        config_entry=other_entry,
    )
    similar = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel_extra",
        config_entry=cloud[0],
    )
    sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    foreign = registry.async_get_or_create(
        "button",
        "other_integration",
        f"{DOMAIN}_foreign_request_exit_real_feel",
        config_entry=cloud[0],
    )
    await real_feel_helpers.setup(hass, cloud)
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(other.entity_id) is not None
    assert registry.async_get(similar.entity_id) is not None
    assert registry.async_get(sensor.entity_id) is not None
    assert registry.async_get(foreign.entity_id) is not None


async def test_reload_does_not_recreate_the_retired_button(hass, cloud):
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    events = []

    @callback
    def on_registry(event):
        if event.data.get("entity_id") == old.entity_id:
            events.append(event.data["action"])

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, on_registry)
    await real_feel_helpers.setup(hass, cloud)
    assert await hass.config_entries.async_reload(cloud[0].entry_id)
    await hass.async_block_till_done()
    assert events == ["remove"]
    assert registry.async_get(old.entity_id) is None
    cloud[4].assert_not_awaited()


async def test_failed_authentication_preserves_the_old_registry_entry(hass, cloud):
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    with patch.object(
        APIClient, "authenticate", AsyncMock(side_effect=InvalidCredentialsError)
    ):
        assert not await hass.config_entries.async_setup(cloud[0].entry_id)
    assert registry.async_get(old.entity_id) is not None


async def test_failed_setup_preserves_the_old_registry_entry(hass, cloud):
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{DOMAIN}_test-dsn_request_exit_real_feel",
        config_entry=cloud[0],
    )
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(side_effect=ConfigEntryNotReady("setup failed")),
    ):
        assert not await hass.config_entries.async_setup(cloud[0].entry_id)
    assert registry.async_get(old.entity_id) is not None


async def test_selector_still_sends_the_tested_exit_request(hass, cloud):
    await real_feel_helpers.setup(hass, cloud)
    await test_real_feel_mode.set_mode(hass, "DEHUMIDIFY")
    cloud[4].assert_awaited_once_with(
        "apiv1/dsns/test-dsn/properties/activate_realfeel/datapoints.json",
        {"datapoint": {"value": REAL_FEEL_DEACTIVATION_PAYLOAD}},
        allow_redirects=False,
    )
