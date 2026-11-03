"""Check translated cloud failures through Home Assistant actions."""

import traceback

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.translation import async_get_translations

from custom_components.delonghi_dehumidifier_api.client import (
    InvalidCredentialsError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests import test_real_feel_mode, test_switch_refresh

device = test_switch_refresh.device


@pytest.mark.parametrize(
    ("failure", "key"),
    [
        (InvalidCredentialsError, "credentials_rejected"),
        (TransientCloudError, "command_connection_failed"),
        (aiohttp.ClientConnectionError, "command_connection_failed"),
        (ValueError, "command_failed"),
    ],
)
async def test_failed_switch_command_has_safe_localized_error(
    hass, device, failure, key
):
    _, _, _, _, send, eco, *_ = device
    send.side_effect = failure("private-cloud-data")
    initial_state = hass.states.get(eco).state
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == key
    assert error.value.translation_placeholders is None
    assert "private-cloud-data" not in "".join(traceback.format_exception(error.value))
    send.assert_awaited_once()
    assert hass.states.get(eco).state == initial_state
    if failure is InvalidCredentialsError:
        assert isinstance(error.value, InvalidCredentialsError)
    english = await async_get_translations(hass, "en", "exceptions", [DOMAIN])
    german = await async_get_translations(hass, "de", "exceptions", [DOMAIN])
    path = f"component.{DOMAIN}.exceptions.{key}.message"
    assert english[path] and german[path] and english[path] != german[path]


def untrusted_error(kind, domain, key):
    error = kind(
        "private-cloud-data",
        translation_domain=domain,
        translation_key=key,
        translation_placeholders={"data": "private-cloud-data"},
    )
    error.__cause__ = ValueError("private-cloud-data")
    return error


def assert_scrubbed(error, expected):
    assert error.translation_domain == DOMAIN
    assert error.translation_key == expected
    assert error.translation_placeholders is None
    assert "private-cloud-data" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize(
    ("kind", "domain", "key", "expected"),
    [
        (HomeAssistantError, "foreign_domain", "foreign_key", "command_failed"),
        (ServiceValidationError, DOMAIN, "unrecognized", "command_failed"),
        (ServiceValidationError, DOMAIN, "unsupported_mode", "unsupported_mode"),
        (
            HomeAssistantError,
            DOMAIN,
            "cloud_permission_denied",
            "cloud_permission_denied",
        ),
    ],
)
async def test_switch_never_exports_untrusted_exception_metadata(
    hass, device, kind, domain, key, expected
):
    _, _, _, _, send, eco, *_ = device
    send.side_effect = untrusted_error(kind, domain, key)
    with pytest.raises(kind) as error:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": eco}, blocking=True
        )
    assert_scrubbed(error.value, expected)
    send.assert_awaited_once()


cloud = test_real_feel_mode.cloud


@pytest.mark.parametrize(
    ("which", "expected"),
    [("read", "real_feel_read_failed"), ("write", "real_feel_write_failed")],
)
async def test_real_feel_scrubs_foreign_errors_and_keeps_recovery_guidance(
    hass, cloud, which, expected
):
    await test_real_feel_mode.setup(hass, cloud)
    _, _, _, get, post = cloud
    target = get if which == "read" else post
    target.side_effect = untrusted_error(
        HomeAssistantError, "foreign_domain", "foreign_key"
    )
    with pytest.raises(HomeAssistantError) as error:
        await test_real_feel_mode.set_mode(hass, "DEHUMIDIFY")
    assert_scrubbed(error.value, expected)
    assert post.await_count == (which == "write")
