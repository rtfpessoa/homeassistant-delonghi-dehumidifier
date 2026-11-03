"""Protect device ownership while setup waits for disk and cloud I/O."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import delonghi_dehumidifier_api as integration
from custom_components.delonghi_dehumidifier_api import identity
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidDeviceListError,
)
from tests import test_account_fence

entry = test_account_fence.entry
cloud = test_account_fence.cloud
provider = test_account_fence.provider


@pytest.mark.parametrize("failure", [False, True])
async def test_restart_during_journal_read_prevents_setup(hass, entry, failure):
    async def read(hass):
        identity.require_restart(hass, "old-entry", "selected")
        if failure:
            raise identity.ClaimJournalError()
        return {}

    with (
        patch.object(integration, "reload_journal", read),
        patch.object(APIClient, "authenticate", AsyncMock()) as auth,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        auth.assert_not_awaited()
    assert test_account_fence.binding_issue(hass, entry) is None


@pytest.mark.parametrize("during_check", [False, True])
async def test_mismatched_registry_identity_requires_repair(
    hass, entry, provider, during_check
):
    def mismatch():
        hass.config_entries.async_update_entry(
            entry, unique_id=identity.device_unique_id("different")
        )

    if not during_check:
        mismatch()
    with patch.object(
        APIClient, "verify_device", AsyncMock(side_effect=lambda _: mismatch())
    ) as verify:
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert verify.await_count == int(during_check)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert test_account_fence.binding_issue(hass, entry) is not None
    entry.async_cancel_retry_setup()


async def test_changed_election_during_verification_prevents_setup(
    hass, entry, provider
):
    async def transfer(dsn):
        await identity.save_election(hass, dsn, "other-entry", set())

    with patch.object(APIClient, "verify_device", AsyncMock(side_effect=transfer)):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert test_account_fence.binding_issue(hass, entry) is not None
    assert (await identity.reload_journal(hass))[entry.unique_id][
        "owner"
    ] == "other-entry"
    entry.async_cancel_retry_setup()


@pytest.mark.parametrize("change", ["restart", "enrollment", "invalid_devices"])
async def test_discovery_cannot_finish_stale_setup(hass, entry, provider, change):
    async def change_during_read():
        if change == "restart":
            identity.require_restart(hass, "old-entry", "selected")
        elif change == "enrollment":
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, identity.CONF_ENROLLMENT: "stale"}
            )
        else:
            raise InvalidDeviceListError()
        return ["selected"]

    with patch.object(
        APIClient, "get_devices", AsyncMock(side_effect=change_during_read)
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is (
        ConfigEntryState.SETUP_ERROR
        if change == "restart"
        else ConfigEntryState.SETUP_RETRY
    )
    if entry.state is ConfigEntryState.SETUP_RETRY:
        entry.async_cancel_retry_setup()


async def test_removed_entry_rejects_direct_setup(hass, entry):
    await hass.config_entries.async_remove(entry.entry_id)
    with pytest.raises(ConfigEntryNotReady):
        await integration.async_setup_entry(hass, entry)
