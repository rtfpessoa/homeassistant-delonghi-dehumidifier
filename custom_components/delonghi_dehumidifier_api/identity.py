"""Serialize device ownership and retain historical registry claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.json import save_json

from .client import usable_dsn
from .const import DOMAIN

CONF_DSN = "device_dsn"
CONF_HISTORICAL_CLAIMS = "historical_device_claims"
ISSUE_PREFIX = "device_binding_"
JOURNAL_KEY = f"{DOMAIN}_claims"
CONF_TRANSFER = "_confirmed_claim_transfer"
CONF_ENROLLMENT = "_pending_device_enrollment"


def reserve_enrollment(hass: HomeAssistant, dsn: str) -> str:
    """Make a live-process-only reservation for an explicit new selection."""
    token = secrets.token_urlsafe(32)
    hass.data.setdefault(f"{DOMAIN}_enrollments", {})[token] = dsn
    return token


def has_enrollment(hass: HomeAssistant, token: str | None, dsn: str) -> bool:
    """Check whether a live reservation token belongs to the selected DSN."""
    return bool(token and hass.data.get(f"{DOMAIN}_enrollments", {}).get(token) == dsn)


def discard_enrollment(hass: HomeAssistant, token: str | None) -> None:
    """Discard a live-process enrollment reservation, if present."""
    hass.data.get(f"{DOMAIN}_enrollments", {}).pop(token, None)


def active_commands(hass: HomeAssistant, entry_id: str) -> int:
    """Count in-flight guarded commands for the entry."""
    return hass.data.get(f"{DOMAIN}_active_commands", {}).get(entry_id, 0)


class ClaimJournalError(Exception):
    """The ownership journal cannot be safely read or written."""


def _valid_claim_record(key: object, record: object) -> bool:
    """Validate one ownership record before any claim is accepted."""
    if not isinstance(key, str) or re.fullmatch(r"device_[0-9a-f]{64}", key) is None:
        return False
    if type(record) is not dict or set(record) != {"owner", "losers"}:
        return False
    owner = record["owner"]
    losers = record["losers"]
    if not isinstance(owner, str) or not owner or type(losers) is not list:
        return False
    if not all(isinstance(item, str) and item for item in losers):
        return False
    return len(set(losers)) == len(losers) and owner not in losers


def _read_journal(path: str) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result

    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file, object_pairs_hook=unique_pairs)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as err:
        raise ClaimJournalError("Cannot read device ownership") from err
    if (
        type(data) is not dict
        or set(data) != {"version", "claims"}
        or type(data.get("version")) is not int
        or data["version"] != 1
        or type(data.get("claims")) is not dict
    ):
        raise ClaimJournalError("Cannot read device ownership")
    claims = data["claims"]
    if any(not _valid_claim_record(key, record) for key, record in claims.items()):
        raise ClaimJournalError("Cannot read device ownership")
    return claims


def _write_journal(path: str, claims: dict) -> None:
    try:
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
            # AtomicWriter syncs the target directory; also make a newly made
            # storage directory durable in its parent before accepting claims.
            fd = os.open(os.path.dirname(directory), os.O_RDONLY)
            try:
                if sys.platform == "darwin":
                    import fcntl

                    fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                else:
                    os.fsync(fd)
            finally:
                os.close(fd)
        save_json(
            path, {"version": 1, "claims": claims}, private=True, atomic_writes=True
        )
    except Exception as err:
        raise ClaimJournalError("Cannot save device ownership") from err


async def load_journal(hass: HomeAssistant) -> dict:
    """Load the journal under the binding lock, without accepting corruption."""
    if JOURNAL_KEY not in hass.data:
        path = hass.config.path(".storage", f"{DOMAIN}.claims")
        hass.data[JOURNAL_KEY] = await hass.async_add_executor_job(_read_journal, path)
    return hass.data[JOURNAL_KEY]


async def reload_journal(hass: HomeAssistant) -> dict:
    """Reconcile external deletion or restoration before a binding decision."""
    hass.data.pop(JOURNAL_KEY, None)
    return await load_journal(hass)


async def save_election(
    hass: HomeAssistant, dsn: str, owner: str, losers: set[str]
) -> None:
    """Durably reserve an election before its entry can be loaded."""
    claims = dict(await load_journal(hass))
    key = device_unique_id(dsn)
    previous = claims.get(key, {})
    historic = set(previous.get("losers", ())) | losers
    if (old_owner := previous.get("owner")) and old_owner != owner:
        historic.add(old_owner)
    historic.discard(owner)
    claims[key] = {"owner": owner, "losers": sorted(historic)}
    path = hass.config.path(".storage", f"{DOMAIN}.claims")
    write = asyncio.ensure_future(
        hass.async_add_executor_job(_write_journal, path, claims)
    )
    cancelled = False
    try:
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                cancelled = True
        write.result()
        if cancelled:
            raise asyncio.CancelledError
    except BaseException:
        hass.data.pop(JOURNAL_KEY, None)
        raise
    hass.data[JOURNAL_KEY] = claims


def device_unique_id(dsn: str) -> str:
    """Use a stable pseudonymous identifier for config entry ownership."""
    digest = hashlib.sha256(f"{DOMAIN}:device:{dsn}".encode()).hexdigest()
    return f"device_{digest}"


def binding_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Return the shared lock used by setup, repair and config flows."""
    key = f"{DOMAIN}_binding_lock"
    if key not in hass.data:
        hass.data[key] = asyncio.Lock()
    return hass.data[key]


def client_settings_match(entry: ConfigEntry, client) -> bool:
    """Compare only effective settings used to authenticate this client."""
    settings = {**entry.data, **entry.options}
    return all(
        settings.get(key) == getattr(client, attribute)
        for key, attribute in (
            (CONF_EMAIL, "email"),
            (CONF_PASSWORD, "password"),
            (CONF_LANGUAGE, "language"),
        )
    )


def _lifecycle_entries(hass: HomeAssistant, name: str) -> set[str]:
    return hass.data.setdefault(f"{DOMAIN}_{name}", set())


def unloading_entries(hass: HomeAssistant) -> set[str]:
    """Entries whose platform unload has started but not completed."""
    return _lifecycle_entries(hass, "unloading")


def restart_required_entries(hass: HomeAssistant) -> set[str]:
    """Entries with potentially partial platform cleanup in this process."""
    return _lifecycle_entries(hass, "restart_required")


def restart_required_devices(hass: HomeAssistant) -> set[str]:
    """Device identities with potentially partial platform cleanup."""
    return _lifecycle_entries(hass, "restart_required_devices")


def device_needs_restart(hass: HomeAssistant, dsn: object) -> bool:
    """Check a verified or selected device without exposing its identifier."""
    return usable_dsn(dsn) and device_unique_id(dsn) in restart_required_devices(hass)


def require_restart(hass: HomeAssistant, entry_id: str, dsn: object) -> None:
    """Fence both the entry and its verified device for this HA process."""
    restart_required_entries(hass).add(entry_id)
    if usable_dsn(dsn):
        restart_required_devices(hass).add(device_unique_id(dsn))


def runtime_is_current(hass: HomeAssistant, entry: ConfigEntry, client) -> bool:
    """Check the live runtime without treating rejected credentials as stale."""
    return (
        hass.config_entries.async_get_entry(entry.entry_id) is entry
        and not entry.disabled_by
        and entry.state is ConfigEntryState.LOADED
        and getattr(entry, "runtime_data", None) is client
        and entry.entry_id not in unloading_entries(hass)
        and entry.entry_id not in restart_required_entries(hass)
        and not client.closed
        and client_settings_match(entry, client)
    )


def registry_dsns(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Include disabled devices and preserve all pre-existing associations."""
    registry = dr.async_get(hass)
    return {
        identifier
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and isinstance(identifier, str) and identifier
    }


def claimants(hass: HomeAssistant, dsn: str) -> list[ConfigEntry]:
    """Find pinned and non-historical registry claimants, including disabled entries."""
    key = device_unique_id(dsn)
    record = hass.data.get(JOURNAL_KEY, {}).get(key, {})
    result = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.data.get(CONF_DSN) == dsn
            or entry.unique_id == key
            or (
                key not in entry.data.get(CONF_HISTORICAL_CLAIMS, ())
                and entry.entry_id not in record.get("losers", ())
                and dsn in registry_dsns(hass, entry)
            )
        ):
            result.append(entry)
    return result


def pending_claim(hass: HomeAssistant, dsn: str, *, exclude: str | None = None) -> bool:
    """Check flow reservations as well as already-created entries."""
    key = device_unique_id(dsn)
    return any(
        flow["flow_id"] != exclude and flow["context"].get("unique_id") == key
        for flow in hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, include_uninitialized=True
        )
    )


def _command_owner_matches(
    hass: HomeAssistant, entry: ConfigEntry, dsn: str, client, record: dict | None
) -> bool:
    """Require the journal, live entry and device binding to agree."""
    if record is None or record["owner"] != entry.entry_id:
        return False
    if entry.data.get(CONF_DSN) != dsn or entry.unique_id != device_unique_id(dsn):
        return False
    return not any(owner is not entry for owner in claimants(hass, dsn))


@asynccontextmanager
async def command_guard(hass: HomeAssistant, entry: ConfigEntry, dsn: str, client):
    """Fence stale owners, and prevent transfer while a device POST is in flight."""
    async with binding_lock(hass):

        def require_current_runtime() -> None:
            if device_needs_restart(hass, dsn):
                raise HomeAssistantError(
                    "Restart Home Assistant before using this integration"
                ) from None
            if (
                not runtime_is_current(hass, entry, client)
                or client.invalid_credentials
            ):
                raise HomeAssistantError(
                    "Integration is changing; try again after reload"
                ) from None

        require_current_runtime()
        try:
            path = hass.config.path(".storage", f"{DOMAIN}.claims")
            journal = await hass.async_add_executor_job(_read_journal, path)
            require_current_runtime()
            hass.data[JOURNAL_KEY] = journal
            record = journal.get(device_unique_id(dsn))
            if not _command_owner_matches(hass, entry, dsn, client, record):
                raise ClaimJournalError("Device ownership needs recovery")
        except ClaimJournalError:
            require_current_runtime()
            create_binding_issue(hass, entry)
            raise HomeAssistantError("Device ownership needs recovery") from None
        commands = hass.data.setdefault(f"{DOMAIN}_active_commands", {})
        commands[entry.entry_id] = commands.get(entry.entry_id, 0) + 1
    try:
        yield
    finally:
        async with binding_lock(hass):
            commands[entry.entry_id] -= 1


def create_binding_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Offer explicit recovery without revealing identifiers in issue metadata."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_PREFIX}{entry.entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_binding",
        data={"entry_id": entry.entry_id},
    )


def clear_binding_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove this entry's resolved ownership issue."""
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PREFIX}{entry.entry_id}")
