"""Exercise complete configuration recovery through the Home Assistant flow manager."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import UnknownFlow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api import config_flow, identity
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    DeviceNotAvailableError,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from tests.identity_helpers import bound_entry

ACCOUNT = {"language": "en", "email": "owner@example.com", "password": "first"}
DEVICE = {identity.CONF_DSN: "test-dsn"}


@pytest.fixture
def account_cloud(single_appliance):
    """Complete a configuration entry without starting device polls."""
    with patch(
        f"custom_components.{DOMAIN}.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        yield


async def select_appliance(hass):
    manager = hass.config_entries.flow
    form = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await manager.async_configure(form["flow_id"], ACCOUNT)


def assert_created(result):
    assert result["type"] == "create_entry"
    entry = result["result"]
    assert entry.data[identity.CONF_DSN] == DEVICE[identity.CONF_DSN]
    # Pin the existing ID so a change to the hash or namespace fails this test.
    assert (
        entry.unique_id
        == "device_ca1c7c8d0a5e5915028bc80a5f6509f3d50b25d085f48a9aa12f5929c4249357"
    )


@pytest.mark.parametrize("step", ["user", "select_device"])
@pytest.mark.parametrize(
    ("method", "failure", "error"),
    [
        ("authenticate", False, "invalid_auth"),
        ("authenticate", InvalidCredentialsError(), "invalid_auth"),
        ("get_devices", TransientCloudError(), "cannot_connect"),
        ("get_devices", NoDevicesError(), "no_devices"),
        ("get_devices", RuntimeError(), "unknown"),
    ],
)
async def test_setup_field_errors_recover_to_a_bound_entry(
    hass, account_cloud, step, method, failure, error
):
    manager = hass.config_entries.flow
    form = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    if step == "select_device":
        form = await manager.async_configure(form["flow_id"], ACCOUNT)
    failing_method = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    with patch.object(APIClient, method, failing_method):
        failed = await manager.async_configure(
            form["flow_id"], ACCOUNT if step == "user" else DEVICE
        )
    assert failed["step_id"] == step and failed["errors"] == {"base": error}
    if step == "user":
        form = await manager.async_configure(form["flow_id"], ACCOUNT)
    assert_created(await manager.async_configure(form["flow_id"], DEVICE))


async def test_selected_device_returns_to_account(hass, account_cloud):
    manager = hass.config_entries.flow
    form = await select_appliance(hass)
    with patch.object(
        APIClient, "get_devices", AsyncMock(return_value=["other-device"])
    ):
        failed = await manager.async_configure(form["flow_id"], DEVICE)
    assert failed["errors"] == {"base": "device_not_available"}
    assert_created(await manager.async_configure(form["flow_id"], DEVICE))


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (False, "unknown"),
        (InvalidCredentialsError(), "invalid_auth"),
        (TimeoutError(), "cannot_connect"),
        (DeviceNotAvailableError(), "device_not_available"),
        (ValueError(), "unknown"),
    ],
)
async def test_reauth_error_can_finish_same_flow(hass, account_cloud, failure, error):
    entry = bound_entry(domain=DOMAIN, data=ACCOUNT)
    entry.add_to_hass(hass)
    unique_id = entry.unique_id
    manager = hass.config_entries.flow
    form = await manager.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    mock_auth = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    input_data = {"password": "replacement"}
    with patch.object(APIClient, "authenticate", mock_auth):
        form = await manager.async_configure(form["flow_id"], input_data)
    assert form["errors"] == {"base": error}
    result = await manager.async_configure(form["flow_id"], input_data)
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.unique_id == unique_id and entry.data["password"] == "replacement"


@pytest.mark.parametrize("as_mapping", [False, True])
async def test_selection_labels_support_both_registry_shapes(
    hass, account_cloud, as_mapping
):
    manager = hass.config_entries.flow
    form = await manager.async_init(DOMAIN, context={"source": SOURCE_USER})
    named = SimpleNamespace(
        name_by_user="Laundry", name=None, identifiers={(DOMAIN, "test-dsn")}
    )
    unnamed = SimpleNamespace(
        name_by_user=None, name=None, identifiers={(DOMAIN, "spare")}
    )
    devices = {"a": named, "b": unnamed} if as_mapping else [named, unnamed]
    with patch.object(
        config_flow.dr, "async_get", return_value=SimpleNamespace(devices=devices)
    ):
        form = await manager.async_configure(form["flow_id"], ACCOUNT)
    selector = next(iter(form["data_schema"].schema.values()))
    assert selector.container == {"test-dsn": "Laundry (test-dsn)"}
    assert_created(await manager.async_configure(form["flow_id"], DEVICE))


async def test_restoring_ownership_journal_recovers_selection(hass, account_cloud):
    manager = hass.config_entries.flow
    form = await select_appliance(hass)
    journal = Path(hass.config.path(".storage", f"{DOMAIN}.claims"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("corrupt")
    failed = await manager.async_configure(form["flow_id"], DEVICE)
    assert failed["step_id"] == "select_device" and failed["errors"] == {
        "base": "ownership_unavailable"
    }
    journal.write_text(json.dumps({"version": 1, "claims": {}}))
    assert_created(await manager.async_configure(form["flow_id"], DEVICE))


@pytest.mark.parametrize("interrupt", ["restart_and_error", "cancel"])
async def test_interrupted_selection_does_not_enroll(hass, account_cloud, interrupt):
    manager = hass.config_entries.flow
    form = await select_appliance(hass)
    started, resume = asyncio.Event(), asyncio.Event()

    async def delayed_read(_hass):
        started.set()
        await resume.wait()
        if interrupt == "restart_and_error":
            raise identity.ClaimJournalError
        return {}

    with patch.object(config_flow, "reload_journal", delayed_read):
        task = asyncio.create_task(manager.async_configure(form["flow_id"], DEVICE))
        await asyncio.wait_for(started.wait(), 5)
        if interrupt == "cancel":
            manager.async_abort(form["flow_id"])
        else:
            identity.require_restart(hass, "previous-entry", DEVICE[identity.CONF_DSN])
        resume.set()
        try:
            result = await task
        except UnknownFlow:
            assert interrupt == "cancel"
        else:
            assert result["reason"] == (
                "reauth_cancelled" if interrupt == "cancel" else "restart_required"
            )
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_journal_owner_with_missing_binding_cannot_be_replaced(
    hass, account_cloud
):
    owner = MockConfigEntry(domain=DOMAIN, data=ACCOUNT)
    owner.add_to_hass(hass)
    await identity.save_election(hass, DEVICE[identity.CONF_DSN], owner.entry_id, set())
    form = await select_appliance(hass)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], DEVICE)
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [owner]


async def test_unresolved_repair_precedes_reconfigure(hass, account_cloud):
    entry = bound_entry(domain=DOMAIN, data=ACCOUNT)
    entry.add_to_hass(hass)
    identity.create_binding_issue(hass, entry)
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert form["type"] == "abort" and form["reason"] == "device_binding_required"
    APIClient.get_access_token.assert_not_awaited()
