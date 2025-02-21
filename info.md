{% if prerelease %}
# This is a pre-release version!
---
{% endif %}

{% if installed %}
## Changes as compared to your installed version:

{% if (version_installed.split(".")[0] | int) < 1 %}
{% if (version_installed.split(".")[1] | int) < 6 %}

## Breaking Changes
- N/A
{% if (version_installed.split(".")[1] | int) < 8 %}
{% endif %}

## Major changes
- TODO

## Bug fixes
- TODO

{% endif %}
{% endif %}
{% endif %}

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]][license]

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]


_Adds support for DeLonghi dehumidifier appliances via API_

**This component will set up the following entities for dehumidifiers.**

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

{% if not installed %}
## Installation

1. Click Install.
2. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "DeLonghi Dehumidifier (API)".

{% endif %}

***

## UI

You may look at following Lovelace cards:

https://github.com/MiguelCosta/Dehumidifier_Comfee_Card


[commits-shield]: https://img.shields.io/github/commit-activity/y/rtfpessoa/homeassistant-delonghi-dehumidifier.svg?style=for-the-badge
[commits]: https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/commits/master
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Default-blue.svg?style=for-the-badge
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license]: https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/blob/main/LICENSE
[license-shield]: https://img.shields.io/github/license/rtfpessoa/homeassistant-delonghi-dehumidifier.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-Nenad%20Bogojević-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/rtfpessoa/homeassistant-delonghi-dehumidifier.svg?style=for-the-badge
[releases]: https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/releases

[user_profile]: https://github.com/rtfpessoa
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start?domain=delonghi_dehumidifier_api
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg

[dehumidifier-details]: https://github.com/rtfpessoa/homeassistant-delonghi-dehumidifier/raw/main/assets/dehumidifier-details.png
