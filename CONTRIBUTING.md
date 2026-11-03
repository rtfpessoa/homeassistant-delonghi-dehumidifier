# Contribution guidelines

Contributing to this project should be as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Submitting a fix
- Proposing new features

## Github is used for everything

Github is used to host code, to track issues and feature requests, as well as accept pull requests.

Pull requests are the best way to propose changes to the codebase.

1. Fork the repo and create your branch from `main`.
2. If you've changed something, update the documentation.
3. Make sure your code lints (using [black](https://pypi.org/project/black/) and [flake8](https://pypi.org/project/flake8/)).
4. Test you contribution.
5. Issue that pull request!

## Any contributions you make will be under the MIT Software License

In short, when you submit code changes, your submissions are understood to be under the same [MIT License](http://choosealicense.com/licenses/mit/) that covers the project. Feel free to contact the maintainers if that's a concern.

## Report bugs using Github's [issues](../../issues)

GitHub issues are used to track public bugs.
Report a bug by [opening a new issue](../../issues/new/choose); it's that easy!

## Write bug reports with detail, background, and sample code

**Great Bug Reports** tend to have:

- A quick summary and/or background
- Steps to reproduce
  - Be specific!
  - Give sample code if you can.
- What you expected would happen
- What actually happens
- Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People *love* thorough bug reports.

## Use a Consistent Coding Style

Use [black](https://github.com/ambv/black) to make sure the code follows the style.

Use [flake8](https://pypi.org/project/flake8/) for linting.

With the current Home Assistant test environment installed, run `python -m mypy`
to check all integration modules against the Home Assistant API. The latest
Home Assistant job in the validation workflow uses the same command.

## Test your code modification

This component comes with development environment in a container, easy to launch
if you use Visual Studio Code. With this container you will have a stand alone
Home Assistant instance running and already configured with the included
[`.devcontainer/configuration.yaml`](./.devcontainer/configuration.yaml)
file.

## Reviewing Platinum readiness

Use `custom_components/delonghi_dehumidifier_api/quality_scale.yaml` for the rule-by-rule assessment. Home Assistant assigns HACS installations the Custom tier. Hassfest validates other parts of custom integrations but skips their quality-scale assessment. Check each rule against its evidence when behavior changes. [Home Assistant quality scale](https://www.home-assistant.io/docs/quality_scale/) · [Hassfest quality-scale validator](https://github.com/home-assistant/core/blob/2026.10.3/script/hassfest/quality_scale.py)

Run the two Home Assistant versions in `.github/workflows/validate.yml` with their matching frontends and test plugins. The cloud tests use simulated devices and local HTTP servers. On the current-version environment, run `python -m mypy` for the entire integration. CI measures coverage per module and checks the config flow on both supported versions.

### Path to an official tier

1. Confirm an authorized, supported way for an independent client to sign in with De’Longhi and Ayla. The bundled implementation uses vendor application credentials. A generic Ayla client can accept a manufacturer SSO token but that still leaves the sign-in exchange to the application. [Ayla SDK guide](https://content.aylanetworks.com/Archive/Ayla-Mobile-Getting-Started.pdf) · [Ayla library](https://pypi.org/project/ayla-iot-unofficial/)
2. Once authentication is established, move the cloud protocol into a standalone async client. Keep Home Assistant state, entities and entry ownership in the integration. Verify the library on the supported appliance and against offline failure cases. Publish a licensed source distribution with `py.typed` from tagged releases in public CI, with a public issue tracker. Pin that release in the manifest. [Core library requirements](https://contributors.home-assistant.io/core/integrations/api-library/)
3. Submit a small Bronze integration to Core with matching documentation and Core brand assets. Custom brand assets already exist for HACS users. Carry the rule evidence forward as features are accepted and submit promotions for review. [Development checklist](https://contributors.home-assistant.io/core/development/checklist/) · [Brand assets](https://github.com/home-assistant/brands/tree/master/custom_integrations/delonghi_dehumidifier_api)
4. For Platinum, register the integration in Core’s `.strict-typing`, regenerate its mypy configuration, verify the published library’s types and request Core team approval. [Strict typing](https://contributors.home-assistant.io/core/quality-scale/strict-typing/) · [Quality scale](https://contributors.home-assistant.io/core/quality-scale/)

## License

By contributing, you agree that your contributions will be licensed under its MIT License.
