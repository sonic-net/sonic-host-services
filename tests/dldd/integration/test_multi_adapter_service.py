from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace

import pytest
import yaml

from dldd.adapters import CLIAdapter, I2CAdapter, adapter_map
from dldd.dse import DSERegistry
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService
from dldd.validation import ExactCompatibilityMatcher
from tests.dldd_fakes import FakeStateDB
from .conftest import (
    BlockingConfigDB,
    CONFIG_VALUES,
    ControlledHashSource,
    NullArtifactClient,
    RunningService,
    SOURCE_KEY,
    eventually,
    integration_rule_document,
)


pytestmark = pytest.mark.dldd_integration


SOURCE_TYPES = frozenset(
    ("redis", "file", "sysfs", "cli", "i2c", "platform_api")
)
CLI_FAULT_KEY = "FAULT_INFO|CLI_SENSOR|SYMPTOM_OVER_THRESHOLD"


class ControlledTransports(object):
    """Thread-safe fake external boundaries for common-monitor adapters."""

    def __init__(self):
        self._values = {
            "cli": "5",
            "i2c": "5",
            "platform_api": "5",
        }
        self.cli_calls = []
        self.i2c_calls = []
        self._lock = RLock()

    def set_value(self, source_type, value):
        with self._lock:
            self._values[source_type] = str(value)

    def run_cli(self, argv, **kwargs):
        with self._lock:
            self.cli_calls.append((tuple(argv), dict(kwargs)))
            value = self._values["cli"]
        return SimpleNamespace(
            returncode=0,
            stdout=(value + "\n").encode("ascii"),
            stderr=b"",
        )

    def read_i2c(self, source):
        with self._lock:
            self.i2c_calls.append(dict(source))
            return self._values["i2c"]

    def read_platform(self):
        with self._lock:
            return self._values["platform_api"]


class ControlledPlatformHook(VendorHook):
    """One registered platform hook covering Platform API and I2C mapping."""

    def __init__(self, transports):
        self.transports = transports
        self.validations = []
        self.platform_calls = []
        self.i2c_bus_calls = []
        self._lock = RLock()

    def validate_source(self, operation):
        with self._lock:
            self.validations.append(dict(operation))

    def collect(self, operation):
        with self._lock:
            self.platform_calls.append(dict(operation))
        return self.transports.read_platform()

    def execute_action(self, action):
        raise AssertionError("multi-adapter integration rule requested an action")

    def resolve_i2c_bus(self, bus, operation):
        with self._lock:
            self.i2c_bus_calls.append((bus, dict(operation)))
        assert bus == "logical-7"
        return "7"


class MultiAdapterIntegrationService(DLDDService):
    """Real service with deterministic substitutes at external I/O only."""

    def __init__(self, redis_source, transports, *args, **kwargs):
        self.redis_source = redis_source
        self.transports = transports
        super(MultiAdapterIntegrationService, self).__init__(*args, **kwargs)

    def _adapters(self):
        adapters = adapter_map(
            hooks=self.extensions.vendor_hooks,
            redis_reader=self.redis_source.read,
        )
        adapters["cli"] = CLIAdapter(runner=self.transports.run_cli)
        adapters["i2c"] = I2CAdapter(
            reader=self.transports.read_i2c,
            hooks=self.extensions.vendor_hooks,
        )
        return adapters

    def _create_artifact_client(self):
        return NullArtifactClient()


def _signature_for(source_type, rule_id, component, path):
    signature = deepcopy(
        integration_rule_document()["signatures"][0]["signature"]
    )
    signature["metadata"].update(
        name="DLDD_INTEGRATION_{}".format(source_type.upper()),
        id=rule_id,
        component=component,
        description="Healthy {} transport integration rule.".format(
            source_type
        ),
    )
    signature["conditions"]["events"][0]["event"].update(
        type=source_type,
        path=path,
    )
    return {"signature": signature}


def _multi_adapter_document(file_path, sysfs_path):
    base = integration_rule_document()
    base["signatures"] = [
        _signature_for(
            "redis",
            9910001,
            "REDIS_SENSOR",
            {
                "database": "STATE_DB",
                "table": "DLDD_TEST_SENSOR",
                "key": SOURCE_KEY,
                "path": "value",
            },
        ),
        _signature_for(
            "file",
            9910002,
            "FILE_SENSOR",
            {"file": str(file_path), "format": "integer"},
        ),
        _signature_for(
            "sysfs",
            9910003,
            "SYSFS_SENSOR",
            {"file": str(sysfs_path), "format": "integer"},
        ),
        _signature_for(
            "cli",
            9910004,
            "CLI_SENSOR",
            {"argv": ["fake-dldd-sensor", "read"], "timeout": 1},
        ),
        _signature_for(
            "i2c",
            9910005,
            "I2C_SENSOR",
            {
                "bus": "logical-7",
                "chip_addr": "0x2a",
                "i2c_type": "get",
                "command": "0x01",
                "size": "b",
            },
        ),
        _signature_for(
            "platform_api",
            9910006,
            "PLATFORM_SENSOR",
            {"hook": "sensor", "channel": "main"},
        ),
    ]
    return base


def _make_service(tmp_path):
    file_path = tmp_path / "file-sensor"
    sysfs_path = tmp_path / "sys" / "devices" / "synthetic-sensor"
    file_path.write_text("5\n", encoding="ascii")
    sysfs_path.parent.mkdir(parents=True)
    sysfs_path.write_text("5\n", encoding="ascii")

    platform_dir = tmp_path / "platform"
    platform_dir.mkdir()
    (platform_dir / "dld_rules.yaml").write_text(
        yaml.safe_dump(
            _multi_adapter_document(file_path, sysfs_path), sort_keys=False
        ),
        encoding="utf-8",
    )
    paths = RulePaths(
        platform_dir=str(platform_dir),
        inbox=str(tmp_path / "runtime" / "inbox" / "dld_rules.yaml"),
        rules_dir=str(tmp_path / "runtime" / "rules"),
        state_file=str(tmp_path / "runtime" / "dld_state.json"),
    )

    state_db = FakeStateDB()
    redis_source = ControlledHashSource(SOURCE_KEY, {"value": "5"})
    transports = ControlledTransports()
    platform_hook = ControlledPlatformHook(transports)
    vendor_hooks = VendorHookRegistry()
    vendor_hooks.register("sensor", platform_hook)
    vendor_hooks.register("i2c", platform_hook)
    extensions = PlatformExtensions(
        PlatformIdentity(
            "test-platform", "TEST-PRODUCT", "TEST-SOFTWARE"
        ),
        DSERegistry(),
        vendor_hooks,
        ExactCompatibilityMatcher(),
    )
    stop_event = Event()
    config_db = BlockingConfigDB(stop_event, CONFIG_VALUES)
    service = MultiAdapterIntegrationService(
        redis_source,
        transports,
        paths=paths,
        config_db=config_db,
        state_db=state_db,
        extensions=extensions,
        stop_event=stop_event,
    )
    return service, state_db, redis_source, transports, platform_hook


def _successful_source_types(service):
    result = set()
    for monitor in tuple(service.monitors):
        items, states = monitor.plan.runtime_snapshot()
        for key, item in items.items():
            if states[key].last_success_timestamp is not None:
                result.add(item.source_type)
    return result


def _monitor_routes(service):
    result = {}
    for monitor in tuple(service.monitors):
        for item in monitor.plan.item_snapshot().values():
            result[item.source_type] = monitor.plan.monitor_type
    return result


def _fault_rows(state_db):
    return {
        key: state_db.hgetall(key)
        for key in state_db.keys("FAULT_INFO|*")
    }


def test_threaded_service_spans_all_direct_adapters_without_false_faults(
    tmp_path,
):
    service, state_db, redis_source, transports, hook = _make_service(tmp_path)
    running = RunningService(service).start()
    try:
        eventually(
            lambda: _successful_source_types(service) == SOURCE_TYPES
        )

        # The generation passed the real Pydantic and activation preflight
        # boundaries, and the planner routed every direct source exactly once.
        assert service.activation.payload.file_valid
        assert not service.activation.payload.broken_rules
        assert len(service.activation.payload.materialized_rules) == 6
        assert {
            item.source_type for item in service.orchestrator.work_items.values()
        } == SOURCE_TYPES
        assert _monitor_routes(service) == {
            "redis": "redis",
            "file": "file",
            "sysfs": "common",
            "cli": "common",
            "i2c": "common",
            "platform_api": "common",
        }

        # Every external transport boundary ran, but six healthy samples did
        # not create a false FAULT_INFO row.
        assert redis_source.read_calls
        assert transports.cli_calls
        assert transports.i2c_calls
        assert hook.platform_calls
        assert hook.i2c_bus_calls
        assert any(
            binding.get("hook") == "sensor"
            for binding in hook.validations
        )
        assert any(
            binding.get("i2c_type") == "get"
            for binding in hook.validations
        )
        cli_argv, cli_kwargs = transports.cli_calls[-1]
        assert cli_argv == ("fake-dldd-sensor", "read")
        assert cli_kwargs["shell"] is False
        assert transports.i2c_calls[-1]["bus"] == "7"
        assert not _fault_rows(state_db)

        assert service._publish_status() is True
        index = state_db.hgetall("DLDD_RULE_STATUS|active")
        assert index["rule_count"] == "6"
        for source_type in SOURCE_TYPES:
            row = state_db.hgetall(
                "DLDD_RULE_STATUS|rule|DLDD_INTEGRATION_{}".format(
                    source_type.upper()
                )
            )
            assert row["health"] == "OK"
            assert row["work_items_total"] == "1"
            assert row["active_faults"] == "0"

        # Drive one transport through a real MATCH -> fault -> NO_MATCH clear
        # cycle while every other transport remains healthy.
        transports.set_value("cli", 20)
        active = eventually(
            lambda: (
                row
                if (
                    row := state_db.hgetall(CLI_FAULT_KEY)
                ).get("status")
                == "ACTIVE"
                else None
            )
        )
        assert active["rule"] == "DLDD_INTEGRATION_CLI"
        assert active["component_type"] == "CLI_SENSOR"
        assert json.loads(active["events"])[0]["value_read"] == "20"
        assert len(_fault_rows(state_db)) == 1
        assert service._publish_status() is True
        assert state_db.hgetall(
            "DLDD_RULE_STATUS|rule|DLDD_INTEGRATION_CLI"
        )["active_faults"] == "1"

        transports.set_value("cli", 5)
        inactive = eventually(
            lambda: (
                row
                if (
                    row := state_db.hgetall(CLI_FAULT_KEY)
                ).get("status")
                == "INACTIVE"
                else None
            )
        )
        assert inactive["origin_time"] == active["origin_time"]
        assert inactive["occurrences"] == active["occurrences"]
        assert _fault_rows(state_db) == {CLI_FAULT_KEY: inactive}
        assert service._publish_status() is True
        cleared_status = state_db.hgetall(
            "DLDD_RULE_STATUS|rule|DLDD_INTEGRATION_CLI"
        )
        assert cleared_status["health"] == "OK"
        assert cleared_status["active_faults"] == "0"
        assert _successful_source_types(service) == SOURCE_TYPES
        assert running.error is None
    finally:
        running.stop()

    persisted = json.loads(
        Path(service.paths.state_file).read_text(encoding="utf-8")
    )
    assert persisted["clean_shutdown"] is True
