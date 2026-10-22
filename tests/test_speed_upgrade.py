"""Exercise speed registry and recorder upgrade behavior with Home Assistant."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import bootstrap, loader
from homeassistant.components.recorder import statistics
from homeassistant.components.recorder.models import statistics as statistics_models
from homeassistant.components.recorder.tasks import StatisticsTask, SynchronizeTask
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import CONF_EMAIL, CONF_LANGUAGE, CONF_PASSWORD, UnitOfSpeed
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    flush_store,
)

from custom_components.delonghi_dehumidifier_api import sensor
from custom_components.delonghi_dehumidifier_api.client import APIClient
from custom_components.delonghi_dehumidifier_api.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Choose recorder database before Home Assistant is created."""
    yield


async def sync_recorder(recorder):
    """Wait for queued work even when the worker has already dequeued it."""
    if "future" in SynchronizeTask.__dataclass_fields__:
        completion = asyncio.get_running_loop().create_future()
        recorder.queue_task(SynchronizeTask(completion))
        await completion
    else:
        completion = asyncio.Event()
        recorder.queue_task(SynchronizeTask(completion))
        await completion.wait()


async def assert_separate_speed_statistics(
    hass, recorder, old_id, raw_id, start, period
):
    """Check old m/s history and new unitless history in the real recorder."""
    metadata = {
        item["statistic_id"]: item
        for item in await statistics.async_list_statistic_ids(hass, {old_id, raw_id})
    }
    assert metadata[old_id]["statistics_unit_of_measurement"] == "m/s"
    assert metadata[raw_id]["statistics_unit_of_measurement"] is None
    for entity_id, since, granularity in (
        (old_id, start, "hour"),
        (raw_id, period, "5minute"),
    ):
        rows = await recorder.async_add_executor_job(
            statistics.statistics_during_period,
            hass,
            since,
            None,
            {entity_id},
            granularity,
            None,
            {"mean"},
        )
        assert rows[entity_id][0]["mean"] == 3.0, entity_id


def assert_raw_speed_measurement(hass, registry, raw_id):
    """The same raw entity reports a unitless measurement."""
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_test-dsn_current_speed_raw_sensor"
        )
        == raw_id
    )
    raw = hass.states.get(raw_id)
    assert raw.state == "3"
    assert "unit_of_measurement" not in raw.attributes
    assert "device_class" not in raw.attributes
    assert raw.attributes["state_class"] == "measurement"


def assert_legacy_speed_preserved(hass, registry, legacy):
    """Keep the legacy registry record and settings without a live value."""
    assert registry.async_get(legacy.entity_id) == legacy
    state = hass.states.get(legacy.entity_id)
    assert state is None or state.state == "unavailable"


@pytest.mark.parametrize(
    "unit", [None, UnitOfSpeed.KILOMETERS_PER_HOUR, "invalid-unit"]
)
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.freeze_time("2026-10-22 12:02:00", real_asyncio=True)
async def test_upgrade_preserves_old_statistics_and_settings(
    recorder_mock, hass, unit, disabled, caplog
):
    caplog.set_level(logging.WARNING)
    values = {
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 3,
        "device_mode": 1,
        "device_status": 1,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
    }

    async def get_properties(_path):
        return [
            {"property": {"name": name, "value": value, "product_name": "Test"}}
            for name, value in values.items()
        ]

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_EMAIL: "a@example.invalid",
            CONF_PASSWORD: "pass",
            CONF_LANGUAGE: "en",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    cls = sensor.GenericSensor

    def legacy_sensor(*args, **kwargs):
        if args[3] == "Current Speed Raw":
            args = (*args[:3], "Current Speed", args[4], SensorDeviceClass.SPEED)
            kwargs["unit_of_measurement"] = UnitOfSpeed.METERS_PER_SECOND
        return cls(*args, **kwargs)

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=get_properties)),
    ):
        with patch.object(sensor, "GenericSensor", legacy_sensor):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        registry = er.async_get(hass)
        old_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_test-dsn_current_speed_sensor"
        )
        assert old_id
        assert (
            hass.states.get(old_id).attributes["unit_of_measurement"]
            == UnitOfSpeed.METERS_PER_SECOND
        )
        assert await hass.config_entries.async_unload(entry.entry_id)

        await hass.async_block_till_done()
        old = registry.async_update_entity(
            old_id,
            new_entity_id="sensor.custom_old_speed",
            name="Custom speed",
            disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
        )
        old_id = old.entity_id
        if unit is not None:
            registry.async_update_entity_options(
                old_id, "sensor", {"unit_of_measurement": unit}
            )
        old = registry.async_get(old_id)
        start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
            hours=2
        )
        meta = {
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": old_id,
            "unit_of_measurement": "m/s",
        }
        if hasattr(statistics_models, "StatisticMeanType"):
            meta.update(
                mean_type=statistics_models.StatisticMeanType.ARITHMETIC,
                unit_class="speed",
            )
        else:
            meta["has_mean"] = True
        statistics.async_import_statistics(
            hass, meta, [{"start": start, "mean": 3.0, "min": 3.0, "max": 3.0}]
        )
        await sync_recorder(recorder_mock)

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert_legacy_speed_preserved(hass, registry, old)
        raw_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_test-dsn_current_speed_raw_sensor"
        )
        assert raw_id and raw_id != old_id
        assert_raw_speed_measurement(hass, registry, raw_id)
        await sync_recorder(recorder_mock)
        now = dt_util.utcnow()
        period = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
        recorder_mock.queue_task(StatisticsTask(period, False))
        await sync_recorder(recorder_mock)
        await assert_separate_speed_statistics(
            hass, recorder_mock, old_id, raw_id, start, period
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert_legacy_speed_preserved(hass, registry, old)
        assert_raw_speed_measurement(hass, registry, raw_id)

        for value, expected in (
            (None, "unknown"),
            ("secret-bad-value", "unavailable"),
            (-2, "-2"),
            (0, "0"),
        ):
            values["current_speed"] = value
            entry.runtime_data.invalidate_properties()
            await async_update_entity(hass, raw_id)
            assert hass.states.get(raw_id).state == expected
        assert "secret-bad-value" not in caplog.text
        assert not any(
            record.levelno >= logging.WARNING
            and record.name.startswith(
                ("homeassistant.components.sensor", "homeassistant.components.recorder")
            )
            and any(
                word in record.getMessage().lower() for word in ("unit", "statistic")
            )
            for record in caplog.records
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("persistent_database", [True])
@pytest.mark.freeze_time("2026-10-22 12:02:00", real_asyncio=True)
async def test_restart_retains_registry_and_statistics(
    async_test_recorder, hass_storage, tmp_path
):
    """Reload stored registries and a persistent recorder in a fresh HA instance."""

    async def setup_registries(hass):
        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        if hasattr(dr, "async_setup"):
            dr.async_setup(hass)
        for registry in (ar, cr, dr, er, fr, ir, lr, rs):
            await registry.async_load(hass)
        hass.data[bootstrap.DATA_REGISTRIES_LOADED] = None

    data = {CONF_EMAIL: "a@example.invalid", CONF_PASSWORD: "pass", CONF_LANGUAGE: "en"}
    properties = {
        "appliance_model": "DDSX220",
        "firmware_version": "1",
        "hardware_version": "1",
        "current_humidity": 50,
        "humidity_setpoint": 55,
        "current_speed": 3,
        "device_mode": 1,
        "device_status": 1,
        "filter_status": 1,
        "filter_change_alarm": 0,
        "filter_life": 90,
        "room_temp": 210,
        "heat_exchanger_temp": 220,
        "set_eco": 0,
        "swing": 0,
        "alarm_state": 0,
    }

    async def get_properties(_path):
        return [
            {"property": {"name": key, "value": value, "product_name": "Test"}}
            for key, value in properties.items()
        ]

    with (
        patch.object(APIClient, "authenticate", AsyncMock(return_value=True)),
        patch.object(APIClient, "get_first_device", AsyncMock(return_value="test-dsn")),
        patch.object(APIClient, "get_request", AsyncMock(side_effect=get_properties)),
    ):
        async with async_test_home_assistant(
            load_registries=False, config_dir=str(tmp_path)
        ) as first:
            await setup_registries(first)
            async with async_test_recorder(first) as recorder:
                entry = MockConfigEntry(domain=DOMAIN, data=data, version=1)
                entry.add_to_hass(first)
                registry = er.async_get(first)
                legacy = registry.async_get_or_create(
                    "sensor",
                    DOMAIN,
                    f"{DOMAIN}_test-dsn_current_speed_sensor",
                    config_entry=entry,
                    original_device_class=SensorDeviceClass.SPEED,
                    unit_of_measurement="m/s",
                )
                legacy = registry.async_update_entity(
                    legacy.entity_id,
                    new_entity_id="sensor.old_speed",
                    name="My old sensor",
                    disabled_by=er.RegistryEntryDisabler.USER,
                )
                registry.async_update_entity_options(
                    legacy.entity_id, "sensor", {"unit_of_measurement": "km/h"}
                )
                legacy = registry.async_get(legacy.entity_id)
                start = dt_util.utcnow().replace(
                    minute=0, second=0, microsecond=0
                ) - timedelta(hours=2)
                meta = {
                    "has_sum": False,
                    "name": None,
                    "source": "recorder",
                    "statistic_id": legacy.entity_id,
                    "unit_of_measurement": "m/s",
                }
                if hasattr(statistics_models, "StatisticMeanType"):
                    meta.update(
                        mean_type=statistics_models.StatisticMeanType.ARITHMETIC,
                        unit_class="speed",
                    )
                else:
                    meta["has_mean"] = True
                statistics.async_import_statistics(
                    first, meta, [{"start": start, "mean": 3.0}]
                )
                await sync_recorder(recorder)
                assert await first.config_entries.async_setup(entry.entry_id)
                await first.async_block_till_done()
                raw_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{DOMAIN}_test-dsn_current_speed_raw_sensor"
                )
                assert raw_id
                assert_raw_speed_measurement(first, registry, raw_id)
                assert_legacy_speed_preserved(first, registry, legacy)
                assert await first.config_entries.async_unload(entry.entry_id)
                await flush_store(registry._store)
                await flush_store(dr.async_get(first)._store)
                entry_id = entry.entry_id
            await first.async_stop()

        async with async_test_home_assistant(
            load_registries=False, config_dir=str(tmp_path)
        ) as second:
            await setup_registries(second)
            async with async_test_recorder(second) as recorder:
                entry = MockConfigEntry(
                    domain=DOMAIN,
                    data=data,
                    version=1,
                    entry_id=entry_id,
                )
                entry.add_to_hass(second)
                registry = er.async_get(second)
                assert registry.async_get(legacy.entity_id) == legacy
                assert await second.config_entries.async_setup(entry.entry_id)
                await second.async_block_till_done()
                assert_raw_speed_measurement(second, registry, raw_id)
                assert_legacy_speed_preserved(second, registry, legacy)
                await sync_recorder(recorder)
                period = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
                recorder.queue_task(StatisticsTask(period, False))
                await sync_recorder(recorder)
                await assert_separate_speed_statistics(
                    second, recorder, legacy.entity_id, raw_id, start, period
                )
                assert await second.config_entries.async_unload(entry.entry_id)
            await second.async_stop()
