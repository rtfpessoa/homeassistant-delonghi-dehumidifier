# DeLonghi Dehumidifier (API)

This custom component for Home Assistant adds support for DeLonghi dehumidifier appliances via API.

[![Repository validation](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/actions/workflows/validate.yml/badge.svg)](https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/actions/workflows/validate.yml)

[![hacs][hacsbadge]][hacs]
[![GitHub Release][releases-shield]][releases]

## Installation instruction

### HACS

The easiest way to install this integration is with [HACS][hacs]. First, install [HACS][hacs-download] if you don't have it yet. In Home Assistant, go to `HACS -> Integrations`, click on `+ Explore & Download Repositories`, search for `DeLonghi Dehumidifier (API)`, and click download. After download, restart Home Assistant.

Once the integration is installed, you can add it to the Home Assistant by going to `Configuration -> Devices & Services`, clicking `+ Add Integration` and searching for `DeLonghi Dehumidifier (API)` or, using My Home Assistant service, you can click on:

[![Add DeLonghi Dehumidifier (API)][add-integration-badge]][add-integration]

### Manual installation

1. Update Home Assistant to version 2025.2.4 or newer.
2. Clone this repository.
3. Copy the `custom_components/delonghi_dehumidifier_api` folder into your Home Assistant's `custom_components` folder.

### Configuring

1. Add `DeLonghi Dehumidifier (API)` integration via UI.
2. Enter DeLonghi language, cloud email and password.
3. The integration will discover the appliance and create the devices.

## Supported appliances

- Tasciugo AriaDry Multi (DDSX220WFA)

Requires account created in DeLonghi cloud.

## Limitations

- Only supports a single device in the cloud account

## Supported entities

This custom component creates following entities for the dehumidifier:

| Platform     | Description                                                                                                                      |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `humidifier` | Dehumidifier entity. Depending on the model following modes are supported: `DEHUMIDIFY`, `DRY_CLOTHES`, `PURIFIER`, `REAL_FEEL`. |
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
| `switch`     | Switch to enable eco mode.                                                                                                       |
| `switch`     | Switch to enable fan swing.                                                                                                      |

## Troubleshooting

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
[hacs-download]: https://hacs.xyz/docs/setup/download
[hacsbadge]: https://img.shields.io/badge/HACS-Default-blue.svg?style=flat
[ha-logs]: https://my.home-assistant.io/redirect/logs
[ha-logs-badge]: https://my.home-assistant.io/badges/logs.svg
[ha-service]: https://my.home-assistant.io/redirect/developer_call_service/?service=logger.set_level
[ha-service-badge]: https://my.home-assistant.io/badges/developer_call_service.svg
[maintenance-shield]: https://img.shields.io/badge/maintainer-Nenad%20Bogojević-blue.svg?style=flat
[releases-shield]: https://img.shields.io/github/release/rtfpessoa/homeassistant-delonghi-dehumidifier.svg?style=flat
[releases]: https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/releases
