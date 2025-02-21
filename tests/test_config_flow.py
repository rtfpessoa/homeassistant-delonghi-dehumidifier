"""Test integration configuration flow"""

# pylint: disable=unused-argument
from typing import Any
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_LANGUAGE,
    CONF_EMAIL,
)
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.delonghi_dehumidifier_api.config_flow import DeLonghiConfigFlow
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
)

MOCK_BASIC_CONFIG_PAGE = {
    CONF_LANGUAGE: "en",
    CONF_EMAIL: "test_email@example.com",
    CONF_PASSWORD: "test_password",
}


async def test_show_form(hass):
    """Test that the form is served with no input."""
    flow = DeLonghiConfigFlow()
    flow.hass = hass

    result: data_entry_flow.FlowResult = await flow.async_step_user(user_input=None)
    print(result)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_successful_config_flow(hass: HomeAssistant, single_appliance):
    """Test a successful config flow."""
    # Initialize a config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Check that the config flow shows the user form as the first step
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_BASIC_CONFIG_PAGE
    )

    # Check that the config flow is complete and a new entry is created with
    # the input data
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "TEST-PRODUCT-NAME"
    assert result["data"][CONF_LANGUAGE] == MOCK_BASIC_CONFIG_PAGE[CONF_LANGUAGE]
    assert result["data"][CONF_EMAIL] == MOCK_BASIC_CONFIG_PAGE[CONF_EMAIL]
    assert result["data"][CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
    assert result["result"]


# async def test_successful_config_flow_midea_two_appliances_only_dehumidifier(
#     hass: HomeAssistant, midea_two_appliances
# ):
#     """Test a successful config flow."""
#     # Initialize a config flow
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )

#     # Check that the config flow shows the user form as the first step
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "user"

#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=MOCK_BASIC_CONFIG_PAGE
#     )

#     # Check that the config flow is complete and a new entry is created with
#     # the input data
#     assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
#     assert result["title"] == "Midea Air Appliance (LAN)"
#     assert result["data"][CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert result["data"][CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert len(result["data"]["devices"]) == 1
#     assert result["result"]


# async def test_advanced_settings_config_flow(hass: HomeAssistant):
#     """Test a advanced settings config flow."""
#     # Initialize a config flow
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#     user_input: dict[str, Any] = {**MOCK_BASIC_CONFIG_PAGE}
#     user_input[CONF_ADVANCED_SETTINGS] = True
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )

#     # Check that the config flow is complete and a new entry is created with
#     # the input data
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "advanced_settings"
#     values = result["data_schema"]({})
#     assert values[CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert values[CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert values[CONF_MOBILE_APP] == DEFAULT_APP
#     assert values[CONF_BROADCAST_ADDRESS] == ""
#     assert values[CONF_INCLUDE] == ["0xa1"]


# async def test_advanced_settings_config_flow_success(
#     hass: HomeAssistant, midea_single_appliances
# ):
#     """Test a advanced settings config flow."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#     user_input: dict[str, Any] = {**MOCK_BASIC_CONFIG_PAGE}
#     user_input[CONF_ADVANCED_SETTINGS] = True
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     user_input = {
#         CONF_USERNAME: "test_username",
#         CONF_PASSWORD: "test_password",
#         CONF_MOBILE_APP: "MSmartHome",
#     }
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
#     assert result["title"] == "Midea Air Appliance (LAN)"
#     assert result["data"][CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert result["data"][CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert result["data"][CONF_MOBILE_APP] == "MSmartHome"
#     assert len(result["data"]["devices"]) == 1
#     assert result["result"]


# async def test_advanced_settings_config_flow_success_network(
#     hass: HomeAssistant, midea_single_appliances
# ):
#     """Test a advanced settings config flow."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#     user_input: dict[str, Any] = {**MOCK_BASIC_CONFIG_PAGE}
#     user_input[CONF_ADVANCED_SETTINGS] = True
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     user_input = {
#         CONF_USERNAME: "test_username",
#         CONF_PASSWORD: "test_password",
#         CONF_MOBILE_APP: "MSmartHome",
#         CONF_BROADCAST_ADDRESS: "192.0.2.255",
#     }
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     print(result)
#     assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
#     assert result["title"] == "Midea Air Appliance (LAN)"
#     assert result["data"]
#     assert result["data"][CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert result["data"][CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert result["data"][CONF_MOBILE_APP] == "MSmartHome"
#     assert result["data"][CONF_BROADCAST_ADDRESS] == ["255.255.255.255", "192.0.2.255"]
#     assert len(result["data"]["devices"]) == 1
#     assert result["result"]


# async def test_advanced_settings_config_invalid_network(hass: HomeAssistant):
#     """Test a advanced settings with invalid network."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#     user_input: dict[str, Any] = {**MOCK_BASIC_CONFIG_PAGE}
#     user_input[CONF_ADVANCED_SETTINGS] = True
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     user_input = {
#         CONF_USERNAME: "test_username",
#         CONF_PASSWORD: "test_password",
#         CONF_BROADCAST_ADDRESS: "655.123.123.333",
#     }
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "advanced_settings"
#     values = result["data_schema"]({})
#     assert values[CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert values[CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert values[CONF_MOBILE_APP] == "NetHome Plus"
#     assert values[CONF_BROADCAST_ADDRESS] == "655.123.123.333"
#     assert result["description_placeholders"]
#     assert (
#         result["description_placeholders"].get("cause")
#         == "Octet 655 (> 255) not permitted in '655.123.123.333'"
#     )
#     assert values[CONF_INCLUDE] == ["0xa1"]


# async def test_advanced_settings_config_flow_success_use_cloud(
#     hass: HomeAssistant, midea_single_appliances
# ):
#     """Test a advanced settings config flow."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )
#     user_input: dict[str, Any] = {**MOCK_BASIC_CONFIG_PAGE}
#     user_input[CONF_ADVANCED_SETTINGS] = True
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     user_input = {
#         CONF_USERNAME: "test_username",
#         CONF_PASSWORD: "test_password",
#         CONF_MOBILE_APP: "MSmartHome",
#         CONF_INCLUDE: ["0xa1", "0xac"],
#     }
#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=user_input
#     )
#     print(result)
#     assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
#     assert result["title"] == "Midea Air Appliance (LAN)"
#     assert result["data"][CONF_USERNAME] == MOCK_BASIC_CONFIG_PAGE[CONF_USERNAME]
#     assert result["data"][CONF_PASSWORD] == MOCK_BASIC_CONFIG_PAGE[CONF_PASSWORD]
#     assert result["data"][CONF_MOBILE_APP] == "MSmartHome"
#     assert result["data"][CONF_INCLUDE] == ["0xa1", "0xac"]
#     assert len(result["data"]["devices"]) == 1
#     assert result["result"]


# async def test_midea_invalid_auth_config_flow(hass: HomeAssistant, midea_invalid_auth):
#     """Test a invalid username config flow."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )

#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=MOCK_BASIC_CONFIG_PAGE
#     )

#     # Check that the config flow is not complete
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "user"
#     assert result["description_placeholders"]
#     assert (
#         result["description_placeholders"].get("cause")
#         == "Cloud authentication error: 45 (34)"
#     )


# async def test_midea_internal_exception(hass: HomeAssistant, midea_internal_exception):
#     """Test an internal exception in midea communication config flow."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )

#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=MOCK_BASIC_CONFIG_PAGE
#     )

#     # Check that the config flow is not complete
#     assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
#     assert result["step_id"] == "user"
#     assert result["description_placeholders"]
#     assert result["description_placeholders"].get("cause") == "midea_internal_exception"


# async def test_config_flow_no_devices(hass: HomeAssistant, midea_no_appliances):
#     """Test a successful config flow with no devices."""
#     result = await hass.config_entries.flow.async_init(
#         DOMAIN, context={"source": config_entries.SOURCE_USER}
#     )

#     result = await hass.config_entries.flow.async_configure(
#         result["flow_id"], user_input=MOCK_BASIC_CONFIG_PAGE
#     )

#     # Check that the config flow is aborted
#     assert result["type"] == data_entry_flow.RESULT_TYPE_ABORT
#     assert result["reason"] == "no_configured_devices"
