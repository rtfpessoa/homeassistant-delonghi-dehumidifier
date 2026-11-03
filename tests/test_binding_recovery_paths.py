"""Exercise failures while users select and confirm device ownership."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.delonghi_dehumidifier_api import identity, repairs
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    NoDevicesError,
)
from tests.test_device_restart_fence import begin_repair


@pytest.mark.parametrize(
    ("authenticated", "failure", "expected"),
    [
        (False, None, "invalid_auth"),
        (True, NoDevicesError(), "no_devices"),
        (True, ValueError(), "unknown"),
    ],
)
async def test_repair_discovery_can_retry(hass, authenticated, failure, expected):
    entry, manager, form = await begin_repair(hass)
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=authenticated)),
        patch.object(APIClient, "get_devices", AsyncMock(side_effect=failure)),
    ):
        retry = await manager.async_configure(
            form["flow_id"], {"discover_other_devices": True}
        )
    assert (retry["step_id"], retry["errors"]) == ("discover", {"base": expected})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["selected"])),
    ):
        form = await manager.async_configure(retry["flow_id"], {})
    assert form["step_id"] == "init"
    assert identity.CONF_DSN not in entry.data
    manager.async_abort(form["flow_id"])


@pytest.mark.parametrize("stage", ["select", "confirm"])
@pytest.mark.parametrize("outcome", ["failed", "fenced", "fenced_and_failed"])
async def test_ownership_read_fails_or_requires_restart(hass, stage, outcome):
    entry, manager, form = await begin_repair(hass)
    selection = {identity.CONF_DSN: "selected"}
    if stage == "confirm":
        form = await manager.async_configure(form["flow_id"], selection)
        form = await manager.async_configure(form["flow_id"], {"owner": entry.entry_id})
    original = repairs.reload_journal

    async def interrupted_read(hass):
        if outcome != "failed":
            identity.require_restart(hass, "old-entry", "selected")
        if outcome != "fenced":
            raise identity.ClaimJournalError()
        return await original(hass)

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "verify_device", AsyncMock()),
        patch.object(repairs, "reload_journal", interrupted_read),
    ):
        result = await manager.async_configure(
            form["flow_id"], selection if stage == "select" else {}
        )
    if outcome == "failed":
        assert result["errors"] == {"base": "ownership_unavailable"}
        assert result["step_id"] == ("init" if stage == "select" else "confirm")
        manager.async_abort(result["flow_id"])
    else:
        assert result["reason"] == "restart_required"
    assert identity.CONF_DSN not in entry.data


@pytest.mark.parametrize("stage", ["owner", "confirm"])
async def test_restart_between_repair_forms_stops_election(hass, stage):
    entry, manager, form = await begin_repair(hass)
    form = await manager.async_configure(
        form["flow_id"], {identity.CONF_DSN: "selected"}
    )
    owner = {"owner": entry.entry_id}
    if stage == "confirm":
        form = await manager.async_configure(form["flow_id"], owner)
    identity.require_restart(hass, "other-entry", "selected")
    result = await manager.async_configure(
        form["flow_id"], owner if stage == "owner" else {}
    )
    assert result["reason"] == "restart_required"
    assert identity.CONF_DSN not in entry.data


@pytest.mark.parametrize(
    ("authenticated", "failure", "error"),
    [
        (False, None, "invalid_auth"),
        (True, InvalidCredentialsError(), "invalid_auth"),
        (True, NoDevicesError(), "device_not_available"),
        (True, ValueError(), "unknown"),
    ],
)
async def test_confirm_cannot_elect_without_verification(
    hass, authenticated, failure, error
):
    entry, manager, form = await begin_repair(hass)
    form = await manager.async_configure(
        form["flow_id"], {identity.CONF_DSN: "selected"}
    )
    form = await manager.async_configure(form["flow_id"], {"owner": entry.entry_id})
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=authenticated)),
        patch.object(APIClient, "verify_device", AsyncMock(side_effect=failure)),
    ):
        retry = await manager.async_configure(form["flow_id"], {})
    assert (retry["step_id"], retry["errors"]) == ("confirm", {"base": error})
    assert identity.CONF_DSN not in entry.data
    hass.config_entries.async_update_entry(entry, options={"language": "de"})
    result = await manager.async_configure(retry["flow_id"], {})
    assert result["reason"] == "settings_changed"
    assert identity.CONF_DSN not in entry.data
