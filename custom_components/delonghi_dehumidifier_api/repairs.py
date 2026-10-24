"""Explicit, non-destructive recovery of legacy device ownership."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, repairs_flow_manager
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import TextSelector, TextSelectorType

from .client import (
    APIClient,
    DeviceNotAvailableError,
    InvalidCredentialsError,
    NoDevicesError,
    TransientCloudError,
    usable_dsn,
)
from .const import DOMAIN
from .identity import (
    CONF_DSN,
    CONF_HISTORICAL_CLAIMS,
    ClaimJournalError,
    active_commands,
    binding_lock,
    claimants,
    create_binding_issue,
    device_needs_restart,
    device_unique_id,
    load_journal,
    pending_claim,
    registry_dsns,
    reload_journal,
    save_election,
)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Create a flow even when the entry itself is retrying."""
    return BindingRepair(data["entry_id"] if data else "")


class BindingRepair(RepairsFlow):
    """Require a privately identified device and an explicit owner election."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._discovery_credentials = {}

    def _registry_candidates(self, entry):
        """Use established registry associations as private selection labels."""
        registry = dr.async_get(self.hass)
        candidates = {}
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            name = device.name_by_user or device.name or "DeLonghi Dehumidifier"
            for domain, dsn in device.identifiers:
                if domain == DOMAIN and usable_dsn(dsn):
                    candidates[dsn] = f"{name} ({dsn})"
        if usable_dsn(pinned := entry.data.get(CONF_DSN)):
            candidates.setdefault(pinned, pinned)
        return candidates

    def _show_init(self, entry, errors=None):
        registry = dr.async_get(self.hass)
        names = [
            device.name_by_user or device.name or "—"
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        ]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DSN): vol.In(self._candidates),
                    vol.Optional("discover_other_devices", default=False): bool,
                }
            ),
            description_placeholders={"devices": ", ".join(names) or "—"},
            errors=errors,
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Select a privately displayed device, discovering when needed."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_removed")
        if not hasattr(self, "_candidates"):
            self._candidates = self._registry_candidates(entry)
            if not self._candidates:
                return await self._discover(entry)
        if user_input is not None and user_input.get("discover_other_devices"):
            return await self._discover(entry)
        if user_input is not None and CONF_DSN in user_input:
            return await self._select_device(entry, user_input[CONF_DSN])
        return self._show_init(
            entry, {"base": "device_not_available"} if user_input is not None else None
        )

    async def _select_device(self, entry, dsn):
        """Load ownership before offering a choice of existing entries."""
        self._dsn = dsn
        if not usable_dsn(self._dsn):
            return self._show_init(entry, {"base": "device_not_available"})
        if device_needs_restart(self.hass, self._dsn):
            return self.async_abort(reason="restart_required")
        try:
            async with binding_lock(self.hass):
                await reload_journal(self.hass)
        except ClaimJournalError:
            if device_needs_restart(self.hass, self._dsn):
                return self.async_abort(reason="restart_required")
            return self._show_init(entry, {"base": "ownership_unavailable"})
        if device_needs_restart(self.hass, self._dsn):
            return self.async_abort(reason="restart_required")
        entries = self.hass.config_entries.async_entries(DOMAIN)
        self._choices = {
            item.entry_id: f"{item.title} ({item.entry_id[-6:]})"
            for item in entries
            if item is entry or item in claimants(self.hass, self._dsn)
        }
        return await self.async_step_owner()

    async def _discover(self, entry):
        """Find identities without controlling a device when no registry exists."""
        settings = {**entry.data, **entry.options, **self._discovery_credentials}
        client = APIClient(
            aiohttp_client.async_get_clientsession(self.hass),
            settings[CONF_LANGUAGE],
            settings[CONF_EMAIL],
            settings[CONF_PASSWORD],
        )
        try:
            if not await client.authenticate():
                error = "invalid_auth"
            else:
                for dsn in await client.get_devices():
                    self._candidates.setdefault(dsn, dsn)
                return self._show_init(entry)
        except InvalidCredentialsError:
            error = "invalid_auth"
        except (aiohttp.ClientError, TimeoutError, TransientCloudError):
            error = "cannot_connect"
        except NoDevicesError:
            error = "no_devices"
        except Exception:  # noqa: BLE001
            error = "unknown"
        finally:
            await client.async_shutdown()
        return self._show_discover({"base": error})

    def _show_discover(self, errors=None):
        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EMAIL): str,
                    vol.Optional(CONF_LANGUAGE): str,
                    vol.Optional(CONF_PASSWORD): TextSelector(
                        {"type": TextSelectorType.PASSWORD}
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_discover(self, user_input: dict[str, Any] | None = None):
        """Retry discovery with optional account credentials."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_removed")
        if user_input is None:
            return self._show_discover()
        self._discovery_credentials = {
            key: user_input[key]
            for key in (CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD)
            if user_input.get(key)
        }
        return await self._discover(entry)

    async def async_step_owner(self, user_input: dict[str, Any] | None = None):
        """Choose an owner and optional credentials, including discovery reuse."""
        if device_needs_restart(self.hass, self._dsn):
            return self.async_abort(reason="restart_required")
        if user_input is not None:
            self._owner_id = user_input["owner"]
            self._credentials = {
                **(
                    self._discovery_credentials
                    if user_input.get("use_discovery_credentials")
                    else {}
                ),
                **{
                    key: user_input[key]
                    for key in (CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD)
                    if user_input.get(key)
                },
            }
            self._snapshot = self._current_snapshot()
            try:
                self._election = (await load_journal(self.hass)).get(
                    device_unique_id(self._dsn)
                )
            except ClaimJournalError:
                if device_needs_restart(self.hass, self._dsn):
                    return self.async_abort(reason="restart_required")
                entry = self.hass.config_entries.async_get_entry(self._entry_id)
                if entry is None:
                    return self.async_abort(reason="entry_removed")
                return self._show_init(entry, {"base": "ownership_unavailable"})
            if device_needs_restart(self.hass, self._dsn):
                return self.async_abort(reason="restart_required")
            return await self.async_step_confirm()
        fields = {
            vol.Required("owner"): vol.In(self._choices),
            vol.Optional(CONF_EMAIL): str,
            vol.Optional(CONF_LANGUAGE): str,
            vol.Optional(CONF_PASSWORD): TextSelector(
                {"type": TextSelectorType.PASSWORD}
            ),
        }
        if self._discovery_credentials:
            fields[vol.Optional("use_discovery_credentials", default=False)] = bool
        return self.async_show_form(
            step_id="owner",
            data_schema=vol.Schema(fields),
        )

    def _current_snapshot(self):
        return {
            entry.entry_id: (
                dict(entry.data),
                dict(entry.options),
                entry.unique_id,
                registry_dsns(self.hass, entry),
            )
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        """Verify account access and elect the selected device's owner."""
        if device_needs_restart(self.hass, self._dsn):
            return self.async_abort(reason="restart_required")
        if user_input is None:
            return self._show_confirm()
        entry = self.hass.config_entries.async_get_entry(self._owner_id)
        if entry is None or self._current_snapshot() != self._snapshot:
            return self.async_abort(reason="settings_changed")
        settings = {**entry.data, **entry.options, **self._credentials}
        pinned = entry.data.get(CONF_DSN)
        if pinned is not None and pinned != self._dsn:
            return self.async_abort(reason="already_bound")

        error = await self._verify_device(settings)
        if error:
            return self._show_confirm(error)
        return await self._elect_owner(entry)

    def _show_confirm(self, error=None):
        """Show the confirmation form, with a retryable error if needed."""
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors={"base": error} if error else None,
        )

    async def _verify_device(self, settings):
        """Verify access without holding the binding lock or changing entries."""
        client = APIClient(
            aiohttp_client.async_get_clientsession(self.hass),
            settings[CONF_LANGUAGE],
            settings[CONF_EMAIL],
            settings[CONF_PASSWORD],
        )
        error = None
        try:
            if not await client.authenticate():
                error = "invalid_auth"
            else:
                await client.verify_device(self._dsn)
        except InvalidCredentialsError:
            error = "invalid_auth"
        except (aiohttp.ClientError, TimeoutError, TransientCloudError):
            error = "cannot_connect"
        except (NoDevicesError, DeviceNotAvailableError):
            error = "device_not_available"
        except Exception:  # noqa: BLE001
            error = "unknown"
        finally:
            await client.async_shutdown()
        return error

    async def _elect_owner(self, entry):
        """Recheck and persist the election before updating entries under lock."""
        async with binding_lock(self.hass):
            manager = repairs_flow_manager(self.hass)
            if device_needs_restart(self.hass, self._dsn):
                return self.async_abort(reason="restart_required")
            try:
                current_election = (await reload_journal(self.hass)).get(
                    device_unique_id(self._dsn)
                )
            except ClaimJournalError:
                if device_needs_restart(self.hass, self._dsn):
                    return self.async_abort(reason="restart_required")
                return self._show_confirm("ownership_unavailable")
            if device_needs_restart(self.hass, self._dsn):
                return self.async_abort(reason="restart_required")
            if self._election_context_is_stale(manager, current_election):
                return self.async_abort(reason="settings_changed")
            owners = claimants(self.hass, self._dsn)
            if self._has_conflicting_owner(entry, owners):
                return self.async_abort(reason="already_bound")
            if entry not in owners and entry.entry_id != self._entry_id:
                return self.async_abort(reason="settings_changed")
            # Persist before touching entries. HA can snapshot entries concurrently
            # on an executor, so a multi-entry update alone is not crash atomic.
            key = device_unique_id(self._dsn)
            try:
                await self._save_election(entry)
            except ClaimJournalError:
                if device_needs_restart(self.hass, self._dsn):
                    return self.async_abort(reason="restart_required")
                return self._show_confirm("ownership_unavailable")
            if device_needs_restart(self.hass, self._dsn):
                return self.async_abort(reason="restart_required")
            if self._current_snapshot() != self._snapshot or not self._flow_is_active(
                manager
            ):
                return self.async_abort(reason="settings_changed")
            self._apply_election(entry, owners, key)
        return self.async_create_entry(data={})

    def _election_context_is_stale(self, manager, current_election):
        """Check the confirmation against current ownership while locked."""
        return (
            manager is None
            or not self._flow_is_active(manager)
            or self._current_snapshot() != self._snapshot
            or pending_claim(self.hass, self._dsn)
            or current_election != self._election
        )

    def _flow_is_active(self, manager):
        return any(
            flow["flow_id"] == self.flow_id
            for flow in manager.async_progress_by_handler(DOMAIN)
        )

    async def _save_election(self, entry):
        """Persist the selected owner and other associated entries."""
        await save_election(
            self.hass,
            self._dsn,
            entry.entry_id,
            {
                other.entry_id
                for other in self.hass.config_entries.async_entries(DOMAIN)
                if other is not entry and self._dsn in registry_dsns(self.hass, other)
            },
        )

    def _has_conflicting_owner(self, entry, owners):
        """Reject an active journal owner or a separately pinned claimant."""
        if (
            self._election
            and self._election["owner"] != entry.entry_id
            and (
                self.hass.config_entries.async_get_entry(self._election["owner"])
                is not None
                or active_commands(self.hass, self._election["owner"])
            )
        ):
            return True
        return any(
            owner is not entry
            and (
                owner.data.get(CONF_DSN) is not None
                or owner.unique_id == device_unique_id(self._dsn)
            )
            for owner in owners
        )

    def _apply_election(self, entry, owners, key):
        """Update entry associations after the durable election is written."""
        for owner in owners:
            if owner is entry:
                continue
            history = set(owner.data.get(CONF_HISTORICAL_CLAIMS, ()))
            history.add(key)
            self.hass.config_entries.async_update_entry(
                owner, data={**owner.data, CONF_HISTORICAL_CLAIMS: sorted(history)}
            )
            create_binding_issue(self.hass, owner)
        history = set(entry.data.get(CONF_HISTORICAL_CLAIMS, ()))
        history.discard(key)
        history.update(
            device_unique_id(dsn)
            for dsn in registry_dsns(self.hass, entry)
            if dsn != self._dsn
        )
        self.hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                **self._credentials,
                CONF_DSN: self._dsn,
                CONF_HISTORICAL_CLAIMS: sorted(history),
            },
            options={
                key: value
                for key, value in entry.options.items()
                if key not in self._credentials
            },
            unique_id=key,
        )
        if not entry.disabled_by:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        if self._entry_id != entry.entry_id:
            issue_entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if issue_entry:
                self.hass.loop.call_soon(create_binding_issue, self.hass, issue_entry)
