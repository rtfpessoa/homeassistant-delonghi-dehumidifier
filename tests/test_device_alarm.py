"""Exercise the observed device alarm values through Home Assistant."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_EMAIL,
    CONF_LANGUAGE,
    CONF_PASSWORD,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

_MISSING = object()


@pytest.fixture
async def device(hass, request):
    """Set up a device with mutable cloud properties."""
    scenario = getattr(request, "param", 0)
    model, alarm = scenario if isinstance(scenario, tuple) else ("DDSX220", scenario)
    properties = {
        "product_name": "Test",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 52,
        "humidity_setpoint": 60,
        "device_mode": 1,
        "device_status": 1,
    }
    if model is not _MISSING:
        properties["appliance_model"] = model
    if alarm is not _MISSING:
        properties["alarm_state"] = alarm

    async def get_properties_request(_path):
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in properties.items()
        ]

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="TEST-DSN")),
        patch.object(
            APIClient, "get_request", AsyncMock(side_effect=get_properties_request)
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        entity_id = er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{DOMAIN}_TEST-DSN_device_alarm_binary_sensor"
        )
        assert entity_id is not None
        yield entry, properties, entity_id
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (0, STATE_OFF),
        (3, STATE_ON),
        (_MISSING, STATE_UNKNOWN),
        (None, STATE_UNKNOWN),
        (1, STATE_UNKNOWN),
        (2, STATE_UNKNOWN),
        (4, STATE_UNKNOWN),
        ("3", STATE_UNKNOWN),
        (3.0, STATE_UNKNOWN),
        (False, STATE_UNKNOWN),
        (True, STATE_UNKNOWN),
        ([3], STATE_UNKNOWN),
        ({"value": 3}, STATE_UNKNOWN),
    ],
    indirect=["device"],
)
async def test_initial_state(hass, device, expected):
    """Only the observed integer values have an interpreted state."""
    _, _, entity_id = device
    state = hass.states.get(entity_id)
    assert state.state == expected
    assert state.attributes["device_class"] == "problem"


async def test_alarm_updates_and_recovers(hass, device):
    """Real entity updates handle transitions, loss of data, and recovery."""
    entry, properties, entity_id = device
    assert hass.states.get(entity_id).state == STATE_OFF

    async def update(value, expected):
        if value is _MISSING:
            properties.pop("alarm_state", None)
        else:
            properties["alarm_state"] = value
        entry.runtime_data.device_properties_timestamp = 0
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).state == expected

    await update(3, STATE_ON)
    await update(99, STATE_UNKNOWN)
    await update(None, STATE_UNKNOWN)
    await update(_MISSING, STATE_UNKNOWN)
    await update(0, STATE_OFF)


async def test_alarm_is_polled(hass, device):
    """The periodic poll refreshes the state from the cloud property."""
    entry, properties, entity_id = device
    assert hass.states.get(entity_id).state == STATE_OFF
    properties["alarm_state"] = 3
    entry.runtime_data.device_properties_timestamp = 0

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON


@pytest.mark.parametrize(
    "device",
    [
        ("DDSX220WFA", 0),
        ("DDSX220WFA", 3),
        ("DDSX220 ", 3),
        ("ddsX220", 3),
        ("another model", 3),
        (_MISSING, 3),
        (None, 3),
        ("", 3),
    ],
    indirect=True,
)
async def test_unverified_or_missing_model_stays_unknown(hass, device):
    """Do not apply the observed model's mapping to other or missing models."""
    _, _, entity_id = device
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_model_disappears_and_returns(hass, device):
    """Missing model information is unknown until the exact model returns."""
    entry, properties, entity_id = device
    assert hass.states.get(entity_id).state == STATE_OFF
    properties.pop("appliance_model")
    properties["alarm_state"] = 3
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_UNKNOWN

    properties["appliance_model"] = "DDSX220"
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_ON


@pytest.mark.parametrize(
    ("device", "error"),
    [
        (0, HomeAssistantError),
        (3, aiohttp.ClientConnectionError),
        (0, TimeoutError),
        (0, InvalidCredentialsError),
        (3, ValueError),
        (0, RuntimeError),
        (3, OSError),
        (0, IndexError),
    ],
    indirect=["device"],
)
async def test_failed_poll_is_unavailable_and_recovers(hass, device, error):
    """A cloud failure must not keep the last healthy or alarm state."""
    entry, properties, entity_id = device
    initial_state = hass.states.get(entity_id).state
    assert initial_state in (STATE_OFF, STATE_ON)
    entry.runtime_data.device_properties_timestamp = 0
    with patch.object(entry.runtime_data, "get_request", AsyncMock(side_effect=error)):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    properties["alarm_state"] = 3 if initial_state == STATE_OFF else 0
    entry.runtime_data.device_properties_timestamp = 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == (
        STATE_ON if initial_state == STATE_OFF else STATE_OFF
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, STATE_UNAVAILABLE),
        ([None], STATE_UNAVAILABLE),
        ([{"property": None}], STATE_UNKNOWN),
        ([], STATE_UNKNOWN),
        ([{"property": {"name": "alarm_state", "value": 3}}], STATE_UNKNOWN),
    ],
)
async def test_bad_response_does_not_keep_old_state(hass, device, response, expected):
    """Malformed or incomplete cloud data cannot leave a stale off state."""
    entry, properties, entity_id = device
    assert hass.states.get(entity_id).state == STATE_OFF
    entry.runtime_data.device_properties_timestamp = 0
    with patch.object(
        entry.runtime_data, "get_request", AsyncMock(return_value=response)
    ):
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
    assert hass.states.get(entity_id).state == expected
    properties["alarm_state"] = 3
    entry.runtime_data.device_properties_timestamp = 0
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_ON
