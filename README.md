# DeLonghi Dehumidifier (API)

This custom component for Home Assistant adds support for DeLonghi dehumidifier appliances via API.

[![Repository validation](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/actions/workflows/validate.yml/badge.svg)](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/actions/workflows/validate.yml)

[![HACS custom repository][hacsbadge]][hacs-custom]

## Installation

### HACS

Requires Home Assistant 2025.2.4 or newer and an installed, configured [HACS][hacs].
See the HACS instructions for [downloading][hacs-download] and [setting up][hacs-setup] HACS.

1. In Home Assistant, open **HACS** from the sidebar.
2. Open the three-dot menu in the top right and select **Custom repositories**.
3. Enter `https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier`, select
   **Integration** as the type, and select **Add**.
4. Find **DeLonghi Dehumidifier (API)** in HACS, open it, and select **Download**.
5. Restart Home Assistant.

Once installed, go to **Settings > Devices & services**, select **Add integration**,
and search for **DeLonghi Dehumidifier (API)**. You can also use My Home Assistant:

[![Add DeLonghi Dehumidifier (API)][add-integration-badge]][add-integration]

### Manual installation

1. Update Home Assistant to version 2025.2.4 or newer.
2. Clone this repository.
3. Copy the `custom_components/delonghi_dehumidifier_api` folder into your Home Assistant's `custom_components` folder.
4. Restart Home Assistant, then follow the configuration steps below.

The automated tests cover Home Assistant 2025.2.4 and 2026.10.3 with a mocked cloud API.

### Versions

Home Assistant shows the installed integration's manifest version on its page
under **Settings > Devices & services**. Without a published GitHub release,
HACS tracks the default branch and identifies updates by commit hash. Publishing
a GitHub release is a separate step from changing the manifest version. Branch
snapshots can share the same manifest version, so include the installed commit
hash when reporting an issue with a branch installation. See the
[release notes](RELEASE_NOTES.md) for upgrade precautions.

### Configuring

1. Add `DeLonghi Dehumidifier (API)` integration via UI.
2. Enter DeLonghi language, cloud email and password.
3. Select the intended device from the discovered IDs. Any matching Home
   Assistant device names are also shown. If there are several devices,
   privately verify which ID belongs to the intended appliance; cloud list
   order alone does not identify it. Do not continue if you cannot verify it.
4. Add another integration entry to use another device with the same account.

Each entry remains bound to its selected device. Changing credentials or the
account language requires that the same device is accessible to the new account.
Devices shared with an account may be read-only; a denied command is reported
without retrying it or switching devices. Discovery does not establish device
type or supported capabilities.

On upgrade, use each entry's Repairs issue to confirm its device, even if the
registry identifies only one. The selector shows known Home Assistant device
names and IDs, or discovers IDs from the account if no association is known.
Existing entries retain their registry device and entity identities when the
account still has access. Privately verify the ID and explicitly choose an
owner. Review the confirmation carefully:
Home Assistant may reassociate existing entities with that owner. Historical
associations are retained, and deleting an entry can trigger Home Assistant's
own cleanup of its entities. A device already bound to an entry cannot be
silently replaced; restore access or add a separate entry for another device.

If platform forwarding or cleanup fails or is interrupted, restart Home
Assistant before loading, repairing or adding the affected device again.
Entities left behind by a failed cleanup may be unavailable.

Before upgrading, persistently disable every entry of this integration, restart
Home Assistant, and verify that the entries remain disabled. Then retain a full
Home Assistant backup including the matching integration,
configuration, registries and ownership journal
(`.storage/delonghi_dehumidifier_api.claims`, if present). You can re-enable
the entries after taking this backup. Restore these components together.
An absent journal may mean a first upgrade, deletion, or an incomplete restore;
each device without its own matching claim requires confirmation, even when
another device has a claim. Missing, mismatched or corrupt claims block control
until recovery or restoration of a matching backup. A complete but stale backup
cannot be distinguished from current data: restoring it intentionally rolls
back the configuration.

Before downgrading, disable or unload all entries of this integration and stop
Home Assistant. While it is stopped, restore the matching pre-upgrade integration
and backup with its entries persistently disabled, including its journal if
present. Verify the entries remain disabled on the first startup after restoring,
then restart Home Assistant again and verify they are still disabled. Restoring
a backup can restore enabled entries, so disabling them before restoration is
not enough.
If the only backup has enabled entries, block De'Longhi/Ayla cloud access at the
network boundary throughout restoration and startup until the entries are
persistently disabled and verified after restart; otherwise, do not restore it.
Never run old integration code with the new multi-entry configuration:
older code ignores the binding and can command a different device if cloud
order changes. Even with a matching pre-upgrade backup, keep old entries
disabled or unloaded while the account has multiple devices. Run old code only
if the account is reliably restricted to the intended single device, or the old
code has been fixed and tested to pin that device. Checking the list order once
does not make subsequent commands safe. Changes made after the backup may need
separate recovery; do not silently overwrite history.

If you also have the `delonghi_dehumidifier_wifi` integration, it has a separate
Home Assistant entry. Adding this API integration does not automatically migrate
the WiFi entities or automations that reference them.

## Supported appliances

- Tasciugo AriaDry Multi (DDSX220WFA)

Requires account created in DeLonghi cloud.

## Limitations

- Capabilities are established only for the tested appliance, not every device
  the cloud account returns. Vendor behavior has not been verified for all
  shared devices or account combinations.

## Supported entities

This custom component creates following entities for the dehumidifier:

| Platform     | Description                                                                                                                      |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `humidifier` | Dehumidifier entity with selectable modes `DEHUMIDIFY`, `DRY_CLOTHES`, and `PURIFIER`; `REAL_FEEL` on the tested DDSX220 firmware. |
| `sensor`     | Sensors for current relative humidity measured by dehumidifier.                                                                  |
| `sensor`     | Sensors for target humidity set in the dehumidifier.                                                                             |
| `sensor`     | Diagnostic sensor for the raw `current_speed` cloud value.                                                                       |
| `sensor`     | Sensors for filter status.                                                                                                       |
| `sensor`     | Sensors for room temperature.                                                                                                    |
| `sensor`     | Sensors for heat exchanger temperature.                                                                                          |
| `sensor`     | Diagnostic sensor for the cloud's raw mode value.                                                                                |
| `sensor`     | Sensors for dehumidifier status.                                                                                                 |
| `sensor`     | Sensors for eco mode.                                                                                                            |
| `sensor`     | Sensors for swing mode.                                                                                                          |
| `sensor`     | Sensors for filter change alarm.                                                                                                 |
| `sensor`     | Sensors for filter life.                                                                                                         |
| `binary_sensor` | Device alarm using observed alarm values.                                                                                      |
| `switch`     | Switch to enable eco mode.                                                                                                       |
| `switch`     | Switch to enable fan swing.                                                                                                      |

The **Current speed (raw)** diagnostic sensor reports the cloud's
`current_speed` number without a unit; its meaning and scale are not established.
It replaces the former **Current Speed** sensor, which labeled the number as m/s
without verification. The old entity and its settings and statistics remain, but it no
longer reports live values. Its m/s label does not establish the meaning of its
history. The new sensor has a separate history; update dashboards and
automations to use its new entity ID.

The **Cloud mode (raw)** diagnostic sensor shows the cloud's `device_mode`
value as a name. On the tested DDSX220, it can read `DEHUMIDIFY` while Real Feel
is active. The humidifier's mode selector shows the integration's best-effort
estimate instead.
Existing entity IDs and user-assigned names stay unchanged. For a new install,
Home Assistant generates the entity ID from the new label.

On a DDSX220 with firmware 1.0, the humidifier offers `REAL_FEEL` in its mode
selector. Selecting `DEHUMIDIFY` while Real Feel is reported sends the tested
exit request; otherwise the integration uses the ordinary mode command. Before
selecting `DEHUMIDIFY`, the integration reads fresh cloud data and rejects the
request if the identity or mode cannot be determined. Entering Real Feel and
exiting to Dehumidify have been observed on one such device. Transitions involving
Purifier have not been verified on the appliance.

The mode is a best-effort estimate based on the cloud's mode value and last
command echo, not a confirmed appliance state. The cloud may lag the appliance,
so check the appliance or De'Longhi app if the modes disagree. After a mode request,
Home Assistant may show an unknown mode while waiting for a matching cloud update.
After two minutes the integration requests another cloud update; a slow or failed
read can leave the mode unknown or unavailable longer. A Dehumidify request may
be rejected during the wait. A different ordinary mode reported by the cloud can
end the wait early; this too is only an estimate. Cloud data can remain stale even
after the wait. Changes from the app do not start this waiting period.

The **Request exit from Real Feel** button has been removed. Update automations
that pressed it to call `humidifier.set_mode` with `DEHUMIDIFY` on the humidifier;
the selector applies its own validation and may reject a request while waiting
for cloud data. Other models and firmware versions do not advertise Real Feel in
the selector. The existing direct `REAL_FEEL` action remains available;
its behavior on other devices and firmware versions has not been established.

The default names of the integration's entities follow Home Assistant's
system language. Their `friendly_name` state attributes can therefore change,
so templates that compare those names may need updating. Home Assistant may use
the system language to generate entity IDs on a new installation. Other existing
entity IDs and user-assigned names are retained when updating the integration.

For a DDSX220, the device alarm uses the API's `alarm_state`: integer `0` is
off, integer `3` is on, and other or missing values are unknown. This mapping
is based on observations of one device: `3` was observed with a full tank and
with the tank removed. The sensor cannot distinguish these conditions, and
other causes for `3` have not been ruled out. Other or missing models report
unknown until their alarm values are understood.

Eco and swing switches update from the cloud during polling. After the cloud
accepts a command, the switch initially shows the requested state; later updates
show the state reported by the cloud, which may lag the device and does not
confirm actuation.

After a Home Assistant control action, the integration requests a shared cloud
update about four seconds after the last action finishes. Rapid actions are
combined into one update. Cloud delays or a failed request can make values take
longer to change. Changes made outside Home Assistant are checked during normal
one-minute polling.

## Troubleshooting

To investigate tank alarms, switch synchronization or operating modes, download
diagnostics from the integration's menu under **Settings > Devices & services**.
Take snapshots after the device has reported each condition, and describe what
you observed.
Allow time for the normal one-minute polling cycle after each change. The
integration includes selected numeric property values, acknowledgement status,
and Real Feel command comparisons from its last successful poll.
`cache_age_seconds` measures time since the request completed, while
`request_age_seconds` measures time since it started.
Home Assistant also includes system and integration information in the download.
For `device_mode`, only integer values from 0 to 255 are shown; other integer
values are indicated by `value_out_of_range` without showing the number.
For `activate_realfeel`, `matches_activation_command` compares the cached value
with this integration's existing activation command.
`matches_candidate_deactivation_command` compares it with a possible exit
command. Neither comparison exports the value, and a match alone does not
confirm the device's mode or the command's effect. A nonmatch does not prove
Real Feel is off.

Debug logging can be activated without going through setup process:

[![Logging service][ha-service-badge]][ha-service]

On entry page, paste following content:

```yaml
service: logger.set_level
data:
  custom_components.delonghi_dehumidifier_api: DEBUG
```

It is possible to activate debug logging on Home Assistent start. To do this, open Home Assistant's `configuration.yaml` file on your machine, and add following to `logger` configuration:

```yaml
logger:
  # Begging of lines to add
  logs:
    custom_components.delonghi_dehumidifier_api: debug
  # End of lines to add
```

Home Assistant needs to be restarted after this change.

Before sharing diagnostics or logs in an [issue](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/issues/new?assignees=&labels=&template=issue.md), inspect and redact them. Remove device identifiers, addresses, credentials, tokens and other private information. Include only excerpts relevant to the problem, not the full Home Assistant log.

The integration's diagnostics contain a limited selection of data, but Home Assistant may add system and integration details to the download. The integration's logging controls do not sanitize logs from Home Assistant or other components, including logs containing chained exceptions.

[![Home Assistant Logs][ha-logs-badge]][ha-logs]

### UI

Following Lovelace cards work well with this integration:

https://github.com/MiguelCosta/Dehumidifier_Comfee_Card

[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=delonghi_dehumidifier_api
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[hacs]: https://hacs.xyz
[hacs-custom]: https://hacs.xyz/docs/faq/custom_repositories/
[hacs-download]: https://hacs.xyz/docs/use/download/download/
[hacs-setup]: https://hacs.xyz/docs/use/configuration/basic/
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-blue.svg?style=flat
[ha-logs]: https://my.home-assistant.io/redirect/logs
[ha-logs-badge]: https://my.home-assistant.io/badges/logs.svg
[ha-service]: https://my.home-assistant.io/redirect/developer_call_service/?service=logger.set_level
[ha-service-badge]: https://my.home-assistant.io/badges/developer_call_service.svg
[maintenance-shield]: https://img.shields.io/badge/maintainer-Nenad%20Bogojević-blue.svg?style=flat
