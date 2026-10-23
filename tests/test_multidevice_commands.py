"""Check that discovery, reads, and commands stay with each selected device."""

import asyncio
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote, urlsplit

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.delonghi_dehumidifier_api import async_migrate_entry
from custom_components.delonghi_dehumidifier_api.client import (
    APIClient,
    DeviceNotAvailableError,
    DeviceNotSelectedError,
    InvalidCredentialsError,
    InvalidDeviceListError,
    Mode,
    NoDevicesError,
    OffOnStatus,
    Status,
)
from custom_components.delonghi_dehumidifier_api.const import (
    DOMAIN,
    REAL_FEEL_ACTIVATION_PAYLOAD,
    REAL_FEEL_DEACTIVATION_PAYLOAD,
)
from custom_components.delonghi_dehumidifier_api.identity import CONF_DSN
from tests.identity_helpers import bound_entry


class Response:
    """Provide a minimal asynchronous HTTP response."""

    def __init__(self, status, body, gate=None, started=None):
        self.status = status
        self.body = body
        self.gate = gate
        self.started = started

    async def __aenter__(self):
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self.body


class Cloud:
    """Record actual client HTTP requests against an account-scoped fake cloud."""

    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []
        self.post_status = 201
        self.get_gate = None
        self.get_started = {}
        self.post_gate = None
        self.post_started = {}

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        token = kwargs["headers"]["Authorization"].removeprefix("auth_token ")
        account = self.accounts[token]
        path = urlsplit(url).path
        if path == "/apiv1/devices.json":
            return Response(200, account["devices"])
        parts = path.split("/")
        assert parts[:3] == ["", "apiv1", "dsns"]
        assert parts[4:] == ["properties.json"]
        dsn = unquote(parts[3])
        assert dsn in account["values"]
        return Response(
            200,
            [
                {
                    "property": {
                        "name": "current_humidity",
                        "value": account["values"][dsn],
                    }
                }
            ],
            self.get_gate,
            self.get_started.get(token),
        )

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        token = kwargs["headers"]["Authorization"].removeprefix("auth_token ")
        return Response(
            self.post_status,
            {},
            self.post_gate,
            self.post_started.get(token),
        )


def devices(*dsns):
    return [{"device": {"dsn": dsn}} for dsn in dsns]


def client_for(cloud, token):
    client = APIClient(cloud, "en", "user@example.test", "password")
    client.access_token = token
    client.token_expiry = time.time() + 600
    return client


def posts(cloud):
    return [call for call in cloud.calls if call[0] == "POST"]


COMMANDS = [
    ("set_status", (Status.ON,), "set_status", 1, None),
    ("set_humidity", (52,), "humidity_setpoint", 52, None),
    ("set_mode", (Mode.DRY_CLOTHES,), "device_mode", 2, None),
    (
        "set_mode",
        (Mode.REAL_FEEL,),
        "activate_realfeel",
        REAL_FEEL_ACTIVATION_PAYLOAD,
        None,
    ),
    (
        "request_enter_real_feel",
        (),
        "activate_realfeel",
        REAL_FEEL_ACTIVATION_PAYLOAD,
        False,
    ),
    (
        "request_exit_real_feel",
        (),
        "activate_realfeel",
        REAL_FEEL_DEACTIVATION_PAYLOAD,
        False,
    ),
    ("set_swing", (OffOnStatus.ON,), "swing", 1, None),
    ("set_eco", (OffOnStatus.ON,), "set_eco", 1, None),
]


@pytest.mark.parametrize("method,args,property_name,value,redirects", COMMANDS)
async def test_every_command_uses_verified_dsn_after_discovery_reorders(
    method, args, property_name, value, redirects
):
    selected = "appliance/A ?#"
    other = "appliance-B"
    cloud = Cloud(
        {"token": {"devices": devices(other, selected), "values": {selected: 54}}}
    )
    client = client_for(cloud, "token")
    try:
        await client.verify_device(selected)
        cloud.accounts["token"]["devices"] = devices(selected, other)
        assert await client.get_devices() == [selected, other]
        assert await client.get_current_humidity() == 54
        await getattr(client, method)(*args)
        assert len(posts(cloud)) == 1
        _, url, kwargs = posts(cloud)[0]
        assert urlsplit(url).path == (
            f"/apiv1/dsns/{quote(selected, safe='')}/properties/"
            f"{property_name}/datapoints.json"
        )
        assert kwargs["json"] == {"datapoint": {"value": value}}
        if redirects is not None:
            assert kwargs["allow_redirects"] is redirects
    finally:
        await client.async_shutdown()


@pytest.mark.parametrize(
    "body,error",
    [
        (devices("other"), DeviceNotAvailableError),
        ([], NoDevicesError),
        ({"device": "other"}, InvalidDeviceListError),
        (devices("selected", "selected"), InvalidDeviceListError),
    ],
)
async def test_failed_verification_never_sends_device_commands(body, error):
    cloud = Cloud({"token": {"devices": body, "values": {}}})
    client = client_for(cloud, "token")
    try:
        with pytest.raises(error):
            await client.verify_device("selected")
        for method, args, _, _, _ in COMMANDS:
            with pytest.raises(DeviceNotSelectedError):
                await getattr(client, method)(*args)
        assert posts(cloud) == []
    finally:
        await client.async_shutdown()


@pytest.mark.parametrize("method,args,property_name,value,redirects", COMMANDS)
async def test_denied_write_is_one_post_to_pin_without_device_switch(
    method, args, property_name, value, redirects
):
    cloud = Cloud({"token": {"devices": devices("other", "selected"), "values": {}}})
    client = client_for(cloud, "token")
    try:
        await client.verify_device("selected")
        cloud.post_status = 403
        with pytest.raises(HomeAssistantError, match="403"):
            await getattr(client, method)(*args)
        assert len(posts(cloud)) == 1
        assert urlsplit(posts(cloud)[0][1]).path == (
            f"/apiv1/dsns/selected/properties/{property_name}/datapoints.json"
        )
        assert await client.get_first_device() == "selected"
        assert len(cloud.calls) == 2  # one discovery and one denied write
    finally:
        await client.async_shutdown()


async def test_offline_migration_performs_no_network(hass):
    data = {
        CONF_EMAIL: "person@example.test",
        CONF_PASSWORD: "password",
        CONF_LANGUAGE: "en",
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data=data, options={CONF_PASSWORD: "override"}, version=1
    )
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "authenticate", AsyncMock()
    ) as authenticate, patch.object(
        APIClient, "get_request", AsyncMock()
    ) as get, patch.object(
        APIClient, "post_request", AsyncMock()
    ) as post:
        assert await async_migrate_entry(hass, entry)
        assert await async_migrate_entry(hass, entry)
    assert dict(entry.data) == data
    assert dict(entry.options) == {CONF_PASSWORD: "override"}
    authenticate.assert_not_called()
    get.assert_not_called()
    post.assert_not_called()


async def test_failed_setup_verification_never_posts_or_forwards(hass):
    cloud = Cloud({"token": {"devices": devices("other"), "values": {}}})
    data = {
        CONF_EMAIL: "person@example.test",
        CONF_PASSWORD: "password",
        CONF_LANGUAGE: "en",
    }
    entry = bound_entry(domain=DOMAIN, data=data, dsn="selected")
    entry.add_to_hass(hass)
    with patch.object(
        APIClient, "get_access_token", AsyncMock(return_value="token")
    ), patch(
        "custom_components.delonghi_dehumidifier_api.aiohttp_client.async_get_clientsession",
        return_value=cloud,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.data[CONF_DSN] == "selected"
    assert posts(cloud) == []
    forward.assert_not_called()
    entry.async_cancel_retry_setup()


async def test_two_clients_keep_requests_actions_auth_and_shutdown_independent(
    monkeypatch,
):
    cloud = Cloud(
        {
            "token-a": {"devices": devices("a", "b"), "values": {"a": 41}},
            "token-b": {"devices": devices("b", "a"), "values": {"b": 62}},
        }
    )
    first = client_for(cloud, "token-a")
    second = client_for(cloud, "token-b")
    pending = []
    try:
        await asyncio.gather(first.verify_device("a"), second.verify_device("b"))
        cloud.get_gate = asyncio.Event()
        cloud.get_started = {token: asyncio.Event() for token in ("token-a", "token-b")}
        pending = [
            asyncio.create_task(client.get_current_humidity())
            for client in (first, first, second, second)
        ]
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in cloud.get_started.values())), 2
        )
        cloud.get_gate.set()
        assert await asyncio.gather(*pending) == [41, 41, 62, 62]
        assert await first.get_current_humidity() == 41
        assert await second.get_current_humidity() == 62
        assert len([call for call in cloud.calls if call[0] == "GET"]) == 4

        cloud.post_gate = asyncio.Event()
        cloud.post_started = {
            token: asyncio.Event() for token in ("token-a", "token-b")
        }
        pending = [
            asyncio.create_task(
                first.humidifier_commands.run(lambda: first.set_status(Status.ON))
            ),
            asyncio.create_task(
                second.humidifier_commands.run(lambda: second.set_humidity(50))
            ),
        ]
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in cloud.post_started.values())), 2
        )
        cloud.post_gate.set()
        await asyncio.gather(*pending)
        assert [urlsplit(call[1]).path for call in posts(cloud)] == [
            "/apiv1/dsns/a/properties/set_status/datapoints.json",
            "/apiv1/dsns/b/properties/humidity_setpoint/datapoints.json",
        ]
        assert await asyncio.gather(
            first.get_current_humidity(), second.get_current_humidity()
        ) == [41, 62]

        monkeypatch.setattr(
            "custom_components.delonghi_dehumidifier_api.action_refresh._REFRESH_DELAY",
            0,
        )
        refresh_a, refresh_b = asyncio.Event(), asyncio.Event()
        first.action_refresh.subscribe(lambda _generation: refresh_a.set())
        second.action_refresh.subscribe(lambda _generation: refresh_b.set())
        await first.humidifier_commands.run(lambda: first.set_status(Status.OFF))
        await asyncio.wait_for(refresh_a.wait(), 2)
        assert not refresh_b.is_set()
        assert await second.get_current_humidity() == 62

        first.access_token = None
        first.get_new_access_token = AsyncMock(
            side_effect=InvalidCredentialsError("rejected")
        )
        first.invalidate_properties()
        with pytest.raises(InvalidCredentialsError):
            await first.get_current_humidity()
        assert first.invalid_credentials
        assert not second.invalid_credentials
        await first.async_shutdown()
        await second.humidifier_commands.run(lambda: second.set_swing(OffOnStatus.ON))
        assert await second.get_current_humidity() == 62
        assert (
            urlsplit(posts(cloud)[-1][1]).path
            == "/apiv1/dsns/b/properties/swing/datapoints.json"
        )
    finally:
        if cloud.get_gate is not None:
            cloud.get_gate.set()
        if cloud.post_gate is not None:
            cloud.post_gate.set()
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await first.async_shutdown()
        await second.async_shutdown()
