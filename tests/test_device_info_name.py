"""Device registry naming tests."""

from unittest.mock import AsyncMock

import pytest

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.utils import fetch_device_info


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        ([], "DeLonghi Dehumidifier"),
        ([{"product_name": "Living Room"}], "Living Room Dehumidifier"),
    ],
)
async def test_device_info_name_with_missing_or_present_product_name(
    properties, expected
):
    """Use a single fallback suffix while preserving names supplied by the cloud."""
    client = APIClient(None, "en", "user@example.com", "password")
    client.get_properties = AsyncMock(return_value=properties)
    client.get_appliance_model = AsyncMock(return_value="DDSX220")
    client.get_firmware_version = AsyncMock(return_value="1.0")
    client.get_hardware_version = AsyncMock(return_value="1")
    client.get_first_device = AsyncMock(return_value="device-123")

    device_info = await fetch_device_info(client)

    assert device_info["name"] == expected
    assert device_info["identifiers"] == {(DOMAIN, "device-123")}
    assert device_info["model"] == "DDSX220"
