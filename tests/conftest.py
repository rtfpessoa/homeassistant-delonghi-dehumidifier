# pylint: disable=protected-access,redefined-outer-name
# pylint: disable=unused-argument
"""Global fixtures for integration."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"  # pylint: disable=invalid-name


@pytest.fixture(autouse=True)
def isolate_ownership_storage(request, tmp_path):
    """Keep durable elections separate from HA's shared test config directory."""
    if "hass" not in request.fixturenames:
        yield
        return
    hass = request.getfixturevalue("hass")
    original_path = hass.config.path

    def path(*parts):
        if parts == (".storage", f"{DOMAIN}.claims"):
            return str(tmp_path / ".storage" / f"{DOMAIN}.claims")
        return original_path(*parts)

    with patch.object(hass.config, "path", side_effect=path):
        yield


# This fixture enables loading custom integrations in all tests.
# Remove to enable selective use of this fixture
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Automatically enable loading custom integrations in all tests."""
    yield


# This fixture is used to prevent HomeAssistant from attempting to create and dismiss
# persistent notifications. These calls would fail without this fixture since the
# persistent_notification integration is never loaded during a test.
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with patch("homeassistant.components.persistent_notification.async_create"), patch(
        "homeassistant.components.persistent_notification.async_dismiss"
    ):
        yield


@pytest.fixture(name="single_appliance")
def single_appliance():
    """Skip calls to get data from API."""
    with patch.multiple(
        APIClient,
        get_access_token=AsyncMock(return_value="TEST-TOKEN"),
        get_product_name=AsyncMock(return_value="TEST-PRODUCT-NAME"),
        get_devices=AsyncMock(return_value=["test-dsn"]),
    ):
        yield


# @pytest.fixture(name="midea_invalid_auth")
# def midea_invalid_auth():
#     """Skip calls to get data from API."""
#     with patch.multiple(
#         APIClient,
#         connect_to_cloud=Mock(
#             side_effect=CloudAuthenticationError(34, "45", "some@example.com")
#         ),
#         appliance_state=Mock(),
#         find_appliances=Mock(return_value=[]),
#     ):
#         yield


# @pytest.fixture(name="midea_internal_exception")
# def midea_internal_exception():
#     """Skip calls to get data from API."""
#     with patch.multiple(
#         APIClient,
#         connect_to_cloud=Mock(side_effect=MideaError("midea_internal_exception")),
#         appliance_state=Mock(),
#         find_appliances=Mock(return_value=[]),
#     ):
#         yield


# @pytest.fixture(name="midea_no_appliances")
# def midea_no_appliances():
#     """Skip calls to get data from API."""
#     with patch.multiple(
#         APIClient,
#         connect_to_cloud=Mock(),
#         appliance_state=Mock(),
#         find_appliances=Mock(return_value=[]),
#     ):
#         yield


# @pytest.fixture(name="midea_two_appliances")
# def midea_two_appliances(dehumidifier_mock, airconditioner_mock):
#     """Skip calls to get data from API."""
#     dehumidifier_mock()
#     with patch.multiple(
#         APIClient,
#         connect_to_cloud=Mock(),
#         appliance_state=Mock(),
#         find_appliances=Mock(return_value=[dehumidifier_mock, airconditioner_mock]),
#     ):
#         yield


# @pytest.fixture(name="airconditioner_mock")
# def airconditioner_mock():
#     """Mock air conditioner appliance"""
#     airconditioner_mock = MagicMock()
#     airconditioner_mock.type = APPLIANCE_TYPE_AIRCON
#     airconditioner_mock.version = 3
#     airconditioner_mock.name = "Test AC"
#     airconditioner_mock.address = "192.0.2.2"
#     airconditioner_mock.token = "TOKENAC"
#     airconditioner_mock.key = "KEYAC"
#     airconditioner_mock.appliance_id = "65432123456"
#     airconditioner_mock.serial_number = "AC654321"
#     return airconditioner_mock


# @pytest.fixture(name="midea_two_appliances_one_supported")
# def midea_two_appliances_one_supported():
#     """Skip calls to get data from API."""
#     with patch.multiple(
#         APIClient,
#         connect_to_cloud=Mock(),
#         appliance_state=Mock(),
#         find_appliances=Mock(return_value=[Mock(), Mock()]),
#     ):
#         yield
