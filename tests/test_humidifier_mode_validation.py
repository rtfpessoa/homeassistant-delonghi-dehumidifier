"""Validate humidifier mode actions through Home Assistant."""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from tests import test_humidifier_sync, test_switch_refresh

device = test_switch_refresh.device
humidifier_id = test_humidifier_sync.humidifier_id


@pytest.mark.parametrize("mode", ["NOT_A_MODE", "dehumidify", ""])
async def test_invalid_mode_rejected_without_a_command(hass, device, mode):
    """Invalid inputs leave device state and the cloud command path untouched."""
    entry, _, _, get, post, *_ = device
    entity_id = humidifier_id(hass)
    state = hass.states.get(entity_id).as_dict()
    generations = (
        entry.runtime_data.humidifier_commands.generation,
        entry.runtime_data._properties_generation,
    )
    reads = get.await_count

    with patch.object(
        entry.runtime_data, "set_mode", wraps=entry.runtime_data.set_mode
    ) as send:
        with pytest.raises(
            ServiceValidationError, match="Unsupported dehumidifier mode"
        ):
            await hass.services.async_call(
                "humidifier",
                "set_mode",
                {"entity_id": entity_id, "mode": mode},
                blocking=True,
            )
        send.assert_not_awaited()

    post.assert_not_awaited()
    assert get.await_count == reads
    assert (
        entry.runtime_data.humidifier_commands.generation,
        entry.runtime_data._properties_generation,
    ) == generations
    assert hass.states.get(entity_id).as_dict() == state


@pytest.mark.parametrize(
    ("mode", "property_name", "expected_value"),
    [
        ("DEHUMIDIFY", "device_mode", 1),
        ("DRY_CLOTHES", "device_mode", 2),
        ("PURIFIER", "device_mode", 3),
        ("REAL_FEEL", "activate_realfeel", "AQIDChIXHEY8Mig="),
    ],
)
async def test_valid_direct_mode_dispatch(
    hass, device, mode, property_name, expected_value
):
    """All existing direct modes retain their cloud command routing."""
    _, _, _, _, post, *_ = device
    await hass.services.async_call(
        "humidifier",
        "set_mode",
        {"entity_id": humidifier_id(hass), "mode": mode},
        blocking=True,
    )
    post.assert_awaited_once()
    post.assert_awaited_once_with(
        f"apiv1/dsns/TEST-DSN/properties/{property_name}/datapoints.json",
        {"datapoint": {"value": expected_value}},
    )


async def test_advertised_modes_stay_unchanged(hass, device):
    """Do not advertise the unverified legacy mode on devices."""
    state = hass.states.get(humidifier_id(hass))
    assert state.attributes["available_modes"] == [
        "DEHUMIDIFY",
        "DRY_CLOTHES",
        "PURIFIER",
    ]
