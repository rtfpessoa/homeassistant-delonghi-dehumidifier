"""Exercise device selection and recovery at Home Assistant's flow boundary."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.data_entry_flow import _BaseFlowManagerView
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    InvalidCredentialsError,
    TransientCloudError,
)
from custom_components.delonghi_dehumidifier_api.const import DOMAIN
from custom_components.delonghi_dehumidifier_api.identity import (
    CONF_DSN,
    create_binding_issue,
)

DATA = {
    CONF_LANGUAGE: "en",
    CONF_EMAIL: "private@example.test",
    CONF_PASSWORD: "secret",
}


def field(form, name):
    """Use HA's actual serializer, as used to construct the frontend form."""
    serialized = _BaseFlowManagerView(None)._prepare_result_json(form)["data_schema"]
    return next(item for item in serialized if item["name"] == name)


@pytest.mark.parametrize("devices", [["private-a"], ["private-a", "private-b"]])
async def test_new_setup_offers_discovered_identities(hass, devices, caplog):
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=devices)),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        form = await hass.config_entries.flow.async_configure(form["flow_id"], DATA)
        choices = field(form, CONF_DSN)
        assert choices["type"] == "select"
        assert {item[0] for item in choices["options"]} == set(devices)
        post.assert_not_awaited()
        assert not hass.config_entries.async_entries(DOMAIN)
        hass.config_entries.flow.async_abort(form["flow_id"])
    assert not any(
        "private-a" in r.getMessage() or "secret" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("properties", [[], [{}], [{"product_name": None}]])
async def test_setup_with_missing_cloud_name_still_creates_entry(hass, properties):
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["private-a"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=properties)),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        form = await hass.config_entries.flow.async_configure(form["flow_id"], DATA)
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_DSN: "private-a"}
        )
        assert result["type"] == "create_entry"
        assert result["title"] == "DeLonghi Dehumidifier"
        assert result["result"].data[CONF_DSN] == "private-a"
        post.assert_not_awaited()
        await result["result"].runtime_data.async_shutdown()


@pytest.mark.parametrize("count", [1, 2])
async def test_legacy_repair_shows_associated_device_names_without_network(hass, count):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    for index in range(count):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"private-{index}")},
            name=f"Room {index}",
        )
    with (
        patch.object(APIClient, "authenticate", AsyncMock()) as auth,
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        choices = dict(field(form, CONF_DSN)["options"])
        assert choices == {
            f"private-{i}": f"Room {i} (private-{i})" for i in range(count)
        }
        assert form["type"] == "form"
        assert CONF_DSN not in entry.data
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "private-0"})
        assert "use_discovery_credentials" not in {
            item["name"]
            for item in _BaseFlowManagerView(None)._prepare_result_json(form)[
                "data_schema"
            ]
        }
        auth.assert_not_awaited()
        post.assert_not_awaited()
        manager.async_abort(form["flow_id"])
        entry.async_cancel_retry_setup()


async def test_missing_registry_discovery_retry_and_password_selector(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    with (
        patch.object(
            APIClient,
            "authenticate",
            AsyncMock(side_effect=[InvalidCredentialsError(), True, True, True]),
        ),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["private-a"])),
        patch.object(APIClient, "get_properties", AsyncMock(return_value=[])),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        assert form["step_id"] == "discover"
        assert form["errors"]["base"] == "invalid_auth"
        assert field(form, CONF_PASSWORD)["selector"]["text"]["type"] == "password"
        form = await manager.async_configure(
            form["flow_id"], {CONF_PASSWORD: "new-secret"}
        )
        assert dict(field(form, CONF_DSN)["options"]) == {"private-a": "private-a"}
        assert entry.data == DATA
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "private-a"})
        assert field(form, CONF_PASSWORD)["selector"]["text"]["type"] == "password"
        form = await manager.async_configure(
            form["flow_id"],
            {"owner": entry.entry_id, "use_discovery_credentials": True},
        )
        form = await manager.async_configure(form["flow_id"], {})
        assert form["type"] == "create_entry"
        await hass.async_block_till_done()
        assert entry.data[CONF_PASSWORD] == "new-secret"
        assert entry.data[CONF_DSN] == "private-a"
        post.assert_not_awaited()
        await entry.runtime_data.async_shutdown()


@pytest.mark.parametrize("other_owner", [False, True])
@pytest.mark.parametrize("use_discovery", [False, True])
@pytest.mark.parametrize("override_password", [False, True])
async def test_discovery_credentials_require_opt_in_for_elected_owner(
    hass, other_owner, use_discovery, override_password
):
    """Discovery credentials never silently replace the selected entry's account."""
    first = MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        options={CONF_LANGUAGE: "pt", CONF_PASSWORD: "first-effective"},
        version=1,
        disabled_by=ConfigEntryDisabler.USER,
    )
    second = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LANGUAGE: "de",
            CONF_EMAIL: "owner@example.test",
            CONF_PASSWORD: "owner-saved",
        },
        options={CONF_LANGUAGE: "fr", CONF_PASSWORD: "owner-effective"},
        version=1,
        disabled_by=ConfigEntryDisabler.USER,
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    owner = second if other_owner else first
    original_data = dict(owner.data)
    original_options = dict(owner.options)
    first_original = (dict(first.data), dict(first.options))
    second_original = (dict(second.data), dict(second.options))
    dr.async_get(hass).async_get_or_create(
        config_entry_id=second.entry_id,
        identifiers={(DOMAIN, "private-a")},
        name="Known room",
    )
    assert await async_setup_component(hass, DOMAIN, {})
    create_binding_issue(hass, first)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    seen_credentials = []

    async def authenticate(client):
        seen_credentials.append((client.email, client.password, client.language))
        if len(seen_credentials) == 1:
            raise InvalidCredentialsError()
        return True

    discovery = {CONF_EMAIL: "new@example.test", CONF_PASSWORD: "discovery-secret"}
    owner_changes = {CONF_PASSWORD: "explicit-secret"} if override_password else {}
    chosen_changes = {**(discovery if use_discovery else {}), **owner_changes}
    expected_settings = {**original_data, **original_options, **chosen_changes}
    with (
        patch.object(
            APIClient, "authenticate", autospec=True, side_effect=authenticate
        ),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["private-a"])),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{first.entry_id}"}
        )
        assert form["step_id"] == "discover"
        form = await manager.async_configure(form["flow_id"], discovery)
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "private-a"})
        assert field(form, "use_discovery_credentials")["default"] is False
        assert field(form, CONF_PASSWORD)["selector"]["text"]["type"] == "password"
        assert "discovery-secret" not in str(
            _BaseFlowManagerView(None)._prepare_result_json(form)
        )
        form = await manager.async_configure(
            form["flow_id"],
            {
                "owner": owner.entry_id,
                **({"use_discovery_credentials": True} if use_discovery else {}),
                **owner_changes,
            },
        )
        form = await manager.async_configure(form["flow_id"], {})
        assert form["type"] == "create_entry"
        assert seen_credentials[-1] == (
            expected_settings[CONF_EMAIL],
            expected_settings[CONF_PASSWORD],
            expected_settings[CONF_LANGUAGE],
        )
        assert owner.data == {
            **original_data,
            **chosen_changes,
            CONF_DSN: "private-a",
            "historical_device_claims": [],
        }
        assert owner.options == {
            key: value
            for key, value in original_options.items()
            if key not in chosen_changes
        }
        other = first if other_owner else second
        other_data, other_options = first_original if other_owner else second_original
        assert all(
            other.data.get(key) == other_data[key]
            for key in (CONF_EMAIL, CONF_PASSWORD, CONF_LANGUAGE)
        )
        assert other.options == other_options
        post.assert_not_awaited()


async def test_opted_in_discovery_credentials_are_saved_only_after_verification(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    with (
        patch.object(
            APIClient,
            "authenticate",
            AsyncMock(side_effect=[InvalidCredentialsError(), True, False]),
        ),
        patch.object(APIClient, "get_devices", AsyncMock(return_value=["private-a"])),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        form = await manager.async_configure(
            form["flow_id"], {CONF_PASSWORD: "discovery-secret"}
        )
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "private-a"})
        form = await manager.async_configure(
            form["flow_id"],
            {"owner": entry.entry_id, "use_discovery_credentials": True},
        )
        form = await manager.async_configure(form["flow_id"], {})
        assert form["step_id"] == "confirm"
        assert form["errors"]["base"] == "invalid_auth"
        assert entry.data == DATA and not entry.options
        post.assert_not_awaited()
        manager.async_abort(form["flow_id"])
        entry.async_cancel_retry_setup()


@pytest.mark.parametrize("language", ["en", "de"])
async def test_discovery_credential_consent_translation(hass, language):
    translations = await async_get_translations(hass, language, "issues", [DOMAIN])
    key = f"component.{DOMAIN}.issues.device_binding.fix_flow.step.owner"
    assert translations[f"{key}.description"]
    assert translations[f"{key}.data.use_discovery_credentials"]


@pytest.mark.parametrize(
    "name", ["strings.json", "translations/en.json", "translations/de.json"]
)
def test_fixable_repair_translation_has_no_issue_description(name):
    path = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN / name
    issue = json.loads(path.read_text(encoding="utf-8"))["issues"]["device_binding"]
    assert "description" not in issue
    assert issue["fix_flow"]["step"]["init"]["description"]


@pytest.mark.parametrize(
    ("language", "introduction"),
    [
        ("en", "Existing devices and history are retained."),
        ("de", "Vorhandene Geräte und Verlauf bleiben erhalten."),
    ],
)
async def test_repair_intro_translation_matches_actual_form(
    hass, language, introduction
):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "private-a")},
        name="Living room",
    )
    assert await async_setup_component(hass, DOMAIN, {})
    create_binding_issue(hass, entry)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    form = await manager.async_init(
        DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
    )
    assert form["step_id"] == "init"
    assert form["description_placeholders"] == {"devices": "Living room"}
    translations = await async_get_translations(hass, language, "issues", [DOMAIN])
    key = f"component.{DOMAIN}.issues.device_binding"
    assert translations[f"{key}.title"]
    description = translations[f"{key}.fix_flow.step.init.description"]
    assert introduction in description
    assert "Living room" in description.format(**form["description_placeholders"])
    manager.async_abort(form["flow_id"])


async def test_repair_can_discover_other_devices_without_losing_registry_labels(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "private-a")},
        name="Old room",
    )
    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(
            APIClient, "get_devices", AsyncMock(return_value=["private-a", "private-b"])
        ),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        form = await manager.async_configure(
            form["flow_id"], {"discover_other_devices": True}
        )
        assert dict(field(form, CONF_DSN)["options"]) == {
            "private-a": "Old room (private-a)",
            "private-b": "private-b",
        }
        post.assert_not_awaited()
        manager.async_abort(form["flow_id"])
        entry.async_cancel_retry_setup()


@pytest.mark.parametrize("language", ["en", "de"])
async def test_options_changed_abort_has_translation(hass, language):
    translations = await async_get_translations(hass, language, "options", [DOMAIN])
    assert translations[f"component.{DOMAIN}.options.abort.reauth_settings_changed"]


async def test_repair_discovery_temporary_cloud_error_offers_retry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock(side_effect=TransientCloudError())
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        assert form["step_id"] == "discover"
        assert form["errors"]["base"] == "cannot_connect"
        manager.async_abort(form["flow_id"])
        entry.async_cancel_retry_setup()


async def test_repair_confirmation_temporary_cloud_error_keeps_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, version=1)
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "private-a")}
    )
    with (
        patch.object(
            APIClient, "authenticate", AsyncMock(side_effect=TransientCloudError())
        ),
        patch.object(APIClient, "post_request", AsyncMock()) as post,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(hass, "repairs", {})
        manager = repairs_flow_manager(hass)
        form = await manager.async_init(
            DOMAIN, data={"issue_id": f"device_binding_{entry.entry_id}"}
        )
        form = await manager.async_configure(form["flow_id"], {CONF_DSN: "private-a"})
        form = await manager.async_configure(form["flow_id"], {"owner": entry.entry_id})
        form = await manager.async_configure(form["flow_id"], {})
        assert form["step_id"] == "confirm"
        assert form["errors"]["base"] == "cannot_connect"
        assert entry.data == DATA
        post.assert_not_awaited()
        manager.async_abort(form["flow_id"])
        entry.async_cancel_retry_setup()
