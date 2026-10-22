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

The automated tests cover Home Assistant 2025.2.4 and 2026.10.2 with a mocked cloud API.

### Versions

Home Assistant shows the installed integration's manifest version on its page
under **Settings > Devices & services**. Without a published GitHub release,
HACS tracks the default branch and identifies updates by commit hash. Publishing
a GitHub release is a separate step from changing the manifest version.

### Configuring

1. Add `DeLonghi Dehumidifier (API)` integration via UI.
2. Enter DeLonghi language, cloud email and password.
3. The integration uses the first device returned by the cloud API.

If you also have the `delonghi_dehumidifier_wifi` integration, it has a separate
Home Assistant entry. Adding this API integration does not automatically migrate
the WiFi entities or automations that reference them.

## Supported appliances

- Tasciugo AriaDry Multi (DDSX220WFA)

Requires account created in DeLonghi cloud.

## Limitations

- Only the first device returned by the cloud API is used. If your account has
  multiple devices, the integration does not provide a way to choose one.

## Supported entities

This custom component creates following entities for the dehumidifier:

| Platform     | Description                                                                                                                      |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `humidifier` | Dehumidifier entity with selectable modes `DEHUMIDIFY`, `DRY_CLOTHES`, and `PURIFIER`.                                           |
| `sensor`     | Sensors for current relative humidity measured by dehumidifier.                                                                  |
| `sensor`     | Sensors for target humidity set in the dehumidifier.                                                                             |
| `sensor`     | Sensors for current speed.                                                                                                       |
| `sensor`     | Sensors for filter status.                                                                                                       |
| `sensor`     | Sensors for room temperature.                                                                                                    |
| `sensor`     | Sensors for heat exchanger temperature.                                                                                          |
| `sensor`     | Sensors for dehumidifier mode.                                                                                                   |
| `sensor`     | Sensors for dehumidifier status.                                                                                                 |
| `sensor`     | Sensors for eco mode.                                                                                                            |
| `sensor`     | Sensors for swing mode.                                                                                                          |
| `sensor`     | Sensors for filter change alarm.                                                                                                 |
| `sensor`     | Sensors for filter life.                                                                                                         |
| `binary_sensor` | Device alarm using observed alarm values.                                                                                      |
| `switch`     | Switch to enable eco mode.                                                                                                       |
| `switch`     | Switch to enable fan swing.                                                                                                      |

Real Feel is not currently listed as a selectable humidifier mode in Home Assistant.

The default names of the integration's entities follow Home Assistant's
system language. Their `friendly_name` state attributes can therefore change,
so templates that compare those names may need updating. Home Assistant may use
the system language to generate entity IDs on a new installation. Existing
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

## Troubleshooting

To investigate tank alarms or switch synchronization, download diagnostics from
the integration's menu under **Settings > Devices & services**. Take snapshots
after the device has reported each condition, and describe what you observed.
Allow time for the normal one-minute polling cycle after each change. The
integration includes only selected numeric property values and acknowledgement
status from its last successful poll; `cache_age_seconds` indicates their age.
Home Assistant also includes system and integration information in the download.

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

Select `Load Full Home Assistant Log` to see all debug mode logs. Please include as much logs as possible if you open an [issue](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/issues/new?assignees=&labels=&template=issue.md).

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
