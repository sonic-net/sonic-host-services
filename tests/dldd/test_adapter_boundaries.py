from __future__ import absolute_import

import subprocess
from types import SimpleNamespace

import pytest

from dldd import command_execution as dldd_command_execution
from dldd.adapters import (
    AdapterError,
    CLIAdapter,
    DSEAdapter,
    DataSourceAdapter,
    FileAdapter,
    I2CAdapter,
    PlatformAPIAdapter,
    RedisAdapter,
    SourceUnavailable,
    VendorAdapter,
    _extract_path,
    _normalize,
)
from dldd.command_execution import ShellFreeResult
from dldd.dse import (
    DSEBinding,
    DSEContext,
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSESourceHandle,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import ValueConfig
from dldd.runtime import EvaluationResultType, MonitorWorkItem
from dldd.sonic_hash import SonicHashReaderError


def _item(
    source_type="redis",
    source=None,
    evaluation=None,
    value_config=None,
    **overrides
):
    values = {
        "rule_id": 1000001,
        "rule_name": "ADAPTER_RULE",
        "rule_version": "1.0.0",
        "schema_version": "0.0.1",
        "severity": "WARNING",
        "priority": 1,
        "symptom": "SYMPTOM_OVER_THRESHOLD",
        "error_type": "SENSOR",
        "component_type": "SENSOR",
        "component_name": "SENSOR0",
        "event_id": 1,
        "correlation_key": "1000001:1:SENSOR0",
        "source_id": "SENSOR|0",
        "source_type": source_type,
        "source": source or {},
        "evaluation": evaluation
        or {"type": "comparison", "operator": ">", "value": 4},
        "value_config": value_config or ValueConfig(),
    }
    values.update(overrides)
    return MonitorWorkItem(**values)


def test_value_normalization_and_path_extraction_contract():
    assert _normalize("0x10", ValueConfig(type="int")) == 16
    assert _normalize("yes", ValueConfig(type="boolean")) is True
    assert _normalize(
        '{"value": 7}', ValueConfig(type="json")
    ) == {"value": 7}
    assert _normalize(2, ValueConfig(type="int", scaling=2.5)) == 5.0
    assert _extract_path('{"rails":[{"value":7}]}', "rails/0/value") == 7

    with pytest.raises(ValueError, match="invalid boolean"):
        _normalize("maybe", ValueConfig(type="boolean"))
    with pytest.raises(KeyError, match="cannot traverse"):
        _extract_path("plain", "child")


class CollectingAdapter(DataSourceAdapter):
    source_type = "test"

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def get_value(self, unused_item):
        if self.error:
            raise self.error
        return self.value


def test_data_source_collection_and_failure_contract():
    item = _item(source_type="test", value_config=ValueConfig(type="int"))
    matched = CollectingAdapter([1, 5]).collect(item)
    unavailable = CollectingAdapter(
        error=SourceUnavailable("not present")
    ).collect(item)
    failed = CollectingAdapter(error=RuntimeError("driver crashed")).collect(item)

    assert matched.result is EvaluationResultType.MATCH
    assert matched.value.normalized == [1, 5]
    assert unavailable.result is EvaluationResultType.SOURCE_UNAVAILABLE
    assert unavailable.retryable
    assert failed.result is EvaluationResultType.COLLECTION_ERROR


def test_dse_adapter_comparator_binding_and_expansion_contract():
    reference = parse_reference("sensor:get_threshold()")
    observed = []
    def comparator(actual):
        return actual == 7
    handle = DSEEvaluationHandle(
        reference,
        lambda invocation: (
            observed.append(invocation.binding)
            or ResolvedEvaluation(comparator=comparator)
        ),
    )
    item = _item(
        source_type="dse",
        source={"vendor": "binding"},
        evaluation={"type": "dse", "value_configs": {}},
        dse_context=DSEContext(component="SENSOR"),
        dse_evaluation_handle=handle,
    )

    assert DSEAdapter().get_evaluator(item)["comparator"] is comparator
    assert observed[0].instance == "SENSOR0"
    assert observed[0].source_id == "SENSOR|0"

    source_handle = DSESourceHandle(
        parse_reference("sensor:get_value()"),
        lambda unused_context: DSEExpansionResult(
            (DSEBinding("SENSOR0", "SENSOR|0"),)
        ),
        lambda invocation: invocation.binding.instance,
    )
    with pytest.raises(ValueError, match="expanded instance"):
        DSEAdapter().validate(
            _item(source_type="dse", dse_source_handle=source_handle)
        )

    invalid_template = SimpleNamespace(
        source_handle=DSESourceHandle(
            source_handle.reference,
            lambda unused_context: {"legacy": "mapping"},
            lambda unused_invocation: None,
        ),
        item=SimpleNamespace(dse_context=DSEContext()),
    )
    with pytest.raises(AdapterError, match="DSEExpansionResult"):
        DSEAdapter().expand(invalid_template)


def test_redis_and_file_adapter_primary_contract(tmp_path):
    calls = []

    class HashReader(object):
        def read(self, database, key):
            calls.append((database, key))
            return {"value": {"rails": [{"voltage": "51.5"}]}}

    redis_item = _item(
        source={
            "database": "STATE_DB",
            "table": "SENSOR_INFO",
            "key": "SENSOR_INFO|0",
            "path": ["value", "rails", "0", "voltage"],
        },
        evaluation={"type": "comparison", "operator": ">", "value": 50.0},
        value_config=ValueConfig(type="float", unit="volts"),
    )
    collected = RedisAdapter(hash_reader=HashReader()).collect(redis_item)
    assert collected.result is EvaluationResultType.MATCH
    assert collected.value.normalized == 51.5
    assert calls == [("STATE_DB", "SENSOR_INFO|0")]

    with pytest.raises(ValueError, match="database"):
        RedisAdapter(hash_reader=HashReader()).validate(_item(source={}))

    class FailedHashReader(object):
        def read(self, database, key):
            raise SonicHashReaderError("STATE_DB disconnected")

    with pytest.raises(SourceUnavailable, match="disconnected"):
        RedisAdapter(hash_reader=FailedHashReader()).get_value(redis_item)

    first = tmp_path / "sensor-1"
    second = tmp_path / "sensor-2"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    file_item = _item(
        source_type="file",
        source={"file": str(tmp_path / "sensor-*"), "format": "integer"},
    )
    assert FileAdapter().get_value(file_item) == [1, 2]

    with pytest.raises(ValueError, match="unsupported file format"):
        FileAdapter().validate(
            _item(
                source_type="file",
                source={"file": str(first), "format": "pickle"},
            )
        )

    missing = _item(
        source_type="file", source={"file": str(tmp_path / "missing")}
    )
    with pytest.raises(SourceUnavailable, match="does not exist"):
        FileAdapter().get_value(missing)


def test_cli_and_i2c_shell_free_command_contract(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        failed = argv[0] == "failing-diagnostic"
        return subprocess.CompletedProcess(
            argv,
            2 if failed else 0,
            stdout=b"ready" if not failed else b"",
            stderr=b"denied" if failed else b"",
        )

    cli_item = _item(
        source_type="cli",
        source={"argv": ["diagnostic"], "timeout": 2, "max_output_bytes": 32},
    )
    assert CLIAdapter(runner).get_value(cli_item) == "ready"
    assert calls[0][1]["shell"] is False
    with pytest.raises(ValueError, match="argv"):
        CLIAdapter().validate(_item(source_type="cli", source={"argv": []}))
    with pytest.raises(AdapterError, match="exited 2: denied"):
        CLIAdapter(runner).get_value(
            _item(source_type="cli", source={"argv": ["failing-diagnostic"]})
        )

    source = {
        "i2c_type": "get",
        "bus": "6",
        "chip_addr": "0x58",
        "command": "0x7a",
    }
    monkeypatch.setattr(
        dldd_command_execution,
        "run_shell_free",
        lambda *args, **kwargs: ShellFreeResult(
            ("i2cget",), 0, b"0x80\n", b""
        ),
    )
    assert I2CAdapter._i2cget(source) == "0x80"

    monkeypatch.setattr(
        dldd_command_execution,
        "run_shell_free",
        lambda *args, **kwargs: ShellFreeResult(
            ("i2cget",), 1, b"", b"bus unavailable\n"
        ),
    )
    with pytest.raises(SourceUnavailable, match="bus unavailable"):
        I2CAdapter._i2cget(source)

    observed = []
    adapter = I2CAdapter(
        lambda operation: observed.append(operation["bus"]) or "0x80"
    )
    item = _item(source_type="i2c", source=source)
    adapter.validate(item)
    assert adapter.get_value(item) == "0x80"
    assert observed == ["6"]

    with pytest.raises(ValueError, match="read-only"):
        I2CAdapter().validate(_item(source_type="i2c", source={}))


class RecordingHook(VendorHook):
    def __init__(self, result):
        self.result = result

    def collect(self, source):
        return self.result

    def execute_action(self, action):
        return {}


def test_platform_and_vendor_adapters_dispatch_registered_hooks():
    hooks = VendorHookRegistry()
    hooks.register("sensor", RecordingHook(7))
    hooks.register("vendor_sensor", RecordingHook(8))

    platform_item = _item(
        source_type="platform_api", source={"hook": "sensor", "field": "value"}
    )
    platform = PlatformAPIAdapter(hooks)
    platform.validate(platform_item)
    assert platform.get_value(platform_item) == 7

    vendor_item = _item(source_type="vendor_sensor", source={"field": "value"})
    vendor = VendorAdapter("vendor_sensor", hooks)
    vendor.validate(vendor_item)
    assert vendor.get_value(vendor_item) == 8

    with pytest.raises(ValueError, match="registered hook"):
        PlatformAPIAdapter(hooks).validate(
            _item(source_type="platform_api", source={})
        )
