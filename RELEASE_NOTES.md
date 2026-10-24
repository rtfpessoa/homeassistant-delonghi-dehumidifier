# 0.3.0 release notes — draft

These notes describe the planned changes and must be checked against the final
implementation before release. The manifest version does not mean a release has
been published. Once merged, HACS installations tracking the default branch can
receive the manifest version without a tag or GitHub release. Device binding and
migration have not been verified on real hardware; earlier DDSX220 testing covered
other changes.

- Setup and options distinguish authentication, connection, and empty-account
  failures. Invalid numeric cloud values make the affected state unavailable
  until valid data returns, rather than being silently converted.
- Each integration entry selects and remains bound to one device. Multiple
  devices can be added as separate entries, subject to ownership checks.
  Integration logging and sharing guidance reduce exposure of sensitive data.
- If platform forwarding or cleanup fails or is interrupted, a Home Assistant
  restart may be required before loading, repairing or adding the affected
  device again. Entities left behind by failed cleanup may be unavailable.
- A unitless raw sensor replaces the old speed sensor because the meaning and
  units of `current_speed` are unknown. Existing speed statistics remain separate
  and must not be interpreted as measurements of a known physical speed. Update
  dashboards and automations to use the new raw entity.

## Upgrade and rollback

Before upgrading, disable every De'Longhi entry, restart Home Assistant, and
verify that the entries remain disabled. Retain a full backup containing the
matching integration, configuration, registries, and any ownership journal.
You can re-enable the entries before upgrading. On upgrade, each legacy entry
without a stored device identity requires one-time device confirmation in
Repairs, even if only one device is found or a matching ownership claim exists.
Missing, mismatched, or corrupt claims require recovery; do not mix a journal
with unrelated backups.

Before downgrading, disable or unload all integration entries and stop Home
Assistant. Restore the matching backup with its entries persistently disabled.
Check that they remain disabled after starting and restarting Home Assistant. If
the backup contains enabled entries, keep the De'Longhi cloud unreachable at the
network boundary throughout restoration and startup until the restored entries
are disabled and a restart confirms this; otherwise, do not restore it. Never run
old integration code with the new multi-entry configuration. Keep old entries
disabled or unloaded while the account has multiple devices: old code selects
the first cloud device, even after a matching backup is restored. Changes made
after the backup may need separate recovery.
