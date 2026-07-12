from __future__ import absolute_import

import builtins
import json
import subprocess
from types import SimpleNamespace

import pytest

from dldd import adapters as dldd_adapters
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
from dldd.runtime import (
    CollectedValue,
    EvaluationResultType,
    MonitorWorkItem,
)
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
    for raw, config, expected in (
        (object(), ValueConfig(), None),
        (b"sensor", ValueConfig(type="string", encoding="ascii"), "sensor"),
        (42, ValueConfig(type="string"), "42"),
        ("0x10", ValueConfig(type="int"), 16),
        ("1.25", ValueConfig(type="float"), 1.25),
        (True, ValueConfig(type="boolean"), True),
        ("yes", ValueConfig(type="boolean"), True),
        ("off", ValueConfig(type="boolean"), False),
        ('{"value": 7}', ValueConfig(type="json"), {"value": 7}),
        ({"value": 7}, ValueConfig(type="json"), {"value": 7}),
        (b"raw", ValueConfig(type="bytes"), b"raw"),
        ("raw", ValueConfig(type="bytes", encoding="ascii"), b"raw"),
        (2, ValueConfig(type="int", scaling=2.5), 5.0),
    ):
        normalized = _normalize(raw, config)
        if config.type == "N/A":
            assert normalized is raw
        else:
            assert normalized == expected

    with pytest.raises(ValueError, match="invalid boolean"):
        _normalize("maybe", ValueConfig(type="boolean"))

    invalid_config = SimpleNamespace(
        type="vendor-object", scaling="N/A", encoding="N/A"
    )
    with pytest.raises(ValueError, match="unsupported value type"):
        _normalize("value", invalid_config)

    assert _extract_path("plain", None) == "plain"
    assert _extract_path('{"rails":[{"value":7}]}', "rails/0/value") == 7
    assert _extract_path({"rails": (10, 11)}, ("rails", "1")) == 11

    with pytest.raises(KeyError, match="cannot traverse"):
        _extract_path("plain", "child")
    with pytest.raises(KeyError, match="cannot traverse"):
        _extract_path(42, "child")


class DelegatingAdapter(DataSourceAdapter):
    source_type = "delegating"

    def get_value(self, item):
        return super().get_value(item)


def test_data_source_base_collection_and_error_contract():
    adapter = DelegatingAdapter()
    with pytest.raises(ValueError, match="cannot handle redis"):
        adapter.validate(_item())
    with pytest.raises(NotImplementedError):
        adapter.get_value(_item(source_type="delegating"))


    class ListAdapter(DataSourceAdapter):
        source_type = "list"

        def get_value(self, unused_item):
            return [1, 5]

    item = _item(
        source_type="list",
        value_config=ValueConfig(type="int"),
    )

    result = ListAdapter().collect(item)

    assert result.result is EvaluationResultType.MATCH
    assert result.value.normalized == [1, 5]

    class FailureAdapter(DataSourceAdapter):
        source_type = "failure"

        def __init__(self, error):
            self.error = error

        def get_value(self, unused_item):
            raise self.error

    item = _item(source_type="failure")
    unavailable = FailureAdapter(SourceUnavailable("not present")).collect(item)
    failed = FailureAdapter(RuntimeError("driver crashed")).collect(item)
    invalid_evaluation = FailureAdapter(None)
    invalid_evaluation.get_value = lambda unused_item: 1
    invalid = invalid_evaluation.collect(
        _item(
            source_type="failure",
            evaluation={"type": "comparison", "operator": "???", "value": 1},
        )
    )
    assert unavailable.result is EvaluationResultType.SOURCE_UNAVAILABLE
    assert unavailable.retryable
    assert failed.result is EvaluationResultType.COLLECTION_ERROR
    assert failed.retryable
    assert invalid.result is EvaluationResultType.EVALUATION_ERROR
    assert invalid.retryable is False


def test_dse_adapter_comparator_binding_and_expansion_contract():
    reference = parse_reference("sensor:get_threshold()")
    observed = []
    comparator = lambda actual: actual == 7
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

    evaluator = DSEAdapter().get_evaluator(item)

    assert evaluator["comparator"] is comparator
    assert "operator" not in evaluator
    assert observed[0].instance == "SENSOR0"
    assert observed[0].source_id == "SENSOR|0"

    adapter = DSEAdapter()
    with pytest.raises(ValueError, match="resolved source handle"):
        adapter.validate(_item(source_type="dse"))

    reference = parse_reference("sensor:get_value()")
    binding = DSEBinding("SENSOR0", "SENSOR|0")
    source_handle = DSESourceHandle(
        reference,
        lambda unused_context: DSEExpansionResult((binding,)),
        lambda invocation: invocation.binding.instance,
    )
    no_binding = _item(source_type="dse", dse_source_handle=source_handle)
    with pytest.raises(ValueError, match="expanded instance"):
        adapter.validate(no_binding)

    template = SimpleNamespace(
        source_handle=DSESourceHandle(
            reference,
            lambda unused_context: {"legacy": "mapping"},
            lambda unused_invocation: 1,
        ),
        item=SimpleNamespace(dse_context=DSEContext()),
    )
    with pytest.raises(AdapterError, match="DSEExpansionResult"):
        adapter.expand(template)


def test_redis_and_file_adapter_validation_collection_and_failure_contract(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError, match="either reader or hash_reader"):
        RedisAdapter(lambda *args: {}, hash_reader=object())

    for missing in ("database", "table", "key"):
        source = {
            "database": "STATE_DB",
            "table": "SENSOR_INFO",
            "key": "SENSOR_INFO|0",
        }
        source[missing] = ""
        with pytest.raises(ValueError, match=missing):
            RedisAdapter(lambda *args: {}).validate(_item(source=source))

    source = {
        "database": "STATE_DB",
        "table": "SENSOR_INFO",
        "key": "SENSOR_INFO|0",
        "path": {"not": "a path"},
    }
    with pytest.raises(ValueError, match="path"):
        RedisAdapter(lambda *args: {}).validate(_item(source=source))

    class FailedHashReader(object):
        def read(self, database, key):
            raise SonicHashReaderError("STATE_DB disconnected")

    item = _item(
        source={
            "database": "STATE_DB",
            "table": "SENSOR_INFO",
            "key": "SENSOR_INFO|0",
        }
    )
    with pytest.raises(SourceUnavailable, match="disconnected"):
        RedisAdapter(hash_reader=FailedHashReader()).get_value(item)


    # File-backed sources validate format, collection, and unavailable paths.
    for source, error in (
        ({}, "requires 'file'"),
        ({"file": "/tmp/value", "format": "pickle"}, "unsupported file format"),
        ({"file": "/tmp/value", "encoding": ""}, "encoding"),
    ):
        with pytest.raises(ValueError, match=error):
            FileAdapter().validate(_item(source_type="file", source=source))

    missing = _item(
        source_type="file",
        source={"file": str(tmp_path / "missing-*")},
    )
    with pytest.raises(SourceUnavailable, match="does not exist"):
        FileAdapter().get_value(missing)

    (tmp_path / "sensor-1").write_text("1", encoding="utf-8")
    (tmp_path / "sensor-2").write_text("2", encoding="utf-8")
    multiple = _item(
        source_type="file",
        source={"file": str(tmp_path / "sensor-*"), "format": "integer"},
    )
    assert FileAdapter().get_value(multiple) == [1, 2]


    for format_name, contents, expected in (
        ("text", " value \n", "value"),
        ("yaml", "value: 7\n", {"value": 7}),
        ("int", "0x10\n", 16),
        ("float", "1.25\n", 1.25),
        ("boolean", "true\n", True),
        ("boolean", "0\n", False),
    ):
        path = tmp_path / "value"
        path.write_text(contents, encoding="utf-8")
        item = _item(
            source_type="file",
            source={"file": str(path), "format": format_name},
        )
        assert FileAdapter().get_value(item) == expected

    path = tmp_path / "value"
    path.write_text("maybe", encoding="utf-8")
    boolean_item = _item(
        source_type="file",
        source={"file": str(path), "format": "boolean"},
    )
    with pytest.raises(AdapterError, match="invalid boolean"):
        FileAdapter().get_value(boolean_item)

    unknown_item = _item(
        source_type="file",
        source={"file": str(path), "format": "vendor"},
    )
    with pytest.raises(AdapterError, match="unsupported file format"):
        FileAdapter._read_path(str(path), unknown_item)

    original_import = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    yaml_item = _item(
        source_type="file",
        source={"file": str(path), "format": "yaml"},
    )
    with pytest.raises(AdapterError, match="YAML support is unavailable"):
        FileAdapter().get_value(yaml_item)


def test_cli_and_i2c_command_adapter_validation_and_failure_contract(monkeypatch):
    for source, error in (
        ({"argv": []}, "argv"),
        ({"argv": ["valid", ""]}, "argv"),
        ({"argv": ["valid"], "timeout": True}, "timeout"),
        ({"argv": ["valid"], "timeout": 0}, "timeout"),
        ({"argv": ["valid"], "max_output_bytes": True}, "max_output"),
        ({"argv": ["valid"], "max_output_bytes": 0}, "max_output"),
    ):
        with pytest.raises(ValueError, match=error):
            CLIAdapter().validate(_item(source_type="cli", source=source))

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 2, stdout=b"", stderr=b"denied")

    item = _item(
        source_type="cli",
        source={"argv": ["diagnostic"], "timeout": 2, "max_output_bytes": 32},
    )
    with pytest.raises(AdapterError, match="exited 2: denied"):
        CLIAdapter(runner).get_value(item)
    assert calls[0][1]["shell"] is False


    # I2C collection follows the same shell-free command boundary.
    source = {
        "i2c_type": "get",
        "bus": "6",
        "chip_addr": "0x58",
        "command": "0x7a",
    }
    monkeypatch.setattr(
        dldd_adapters,
        "run_shell_free",
        lambda *args, **kwargs: ShellFreeResult(
            ("i2cget",), 0, b"0x80\n", b""
        ),
    )
    assert I2CAdapter._i2cget(source) == "0x80"

    monkeypatch.setattr(
        dldd_adapters,
        "run_shell_free",
        lambda *args, **kwargs: ShellFreeResult(
            ("i2cget",), 1, b"", b"bus unavailable\n"
        ),
    )
    with pytest.raises(SourceUnavailable, match="bus unavailable"):
        I2CAdapter._i2cget(source)

    for source, error in (
        ({}, "read-only"),
        (
            {"i2c_type": "set", "bus": "6", "chip_addr": "1", "command": "1"},
            "read-only",
        ),
        (
            {"i2c_type": "get", "bus": "", "chip_addr": "1", "command": "1"},
            "requires 'bus'",
        ),
        (
            {"i2c_type": "get", "bus": "6", "chip_addr": "bad", "command": "1"},
            "chip_addr.*integer",
        ),
        (
            {"i2c_type": "get", "bus": "6", "chip_addr": "1", "command": "bad"},
            "command.*integer",
        ),
        (
            {
                "i2c_type": "get",
                "bus": "6",
                "chip_addr": "1",
                "command": "1",
                "size": "q",
            },
            "size",
        ),
        (
            {
                "i2c_type": "get",
                "bus": "6",
                "chip_addr": "1",
                "command": "1",
                "timeout": True,
            },
            "timeout",
        ),
        (
            {
                "i2c_type": "get",
                "bus": "6",
                "chip_addr": "1",
                "command": "1",
                "timeout": 0,
            },
            "timeout",
        ),
    ):
        with pytest.raises(ValueError, match=error):
            I2CAdapter().validate(_item(source_type="i2c", source=source))


class RecordingHook(VendorHook):
    def __init__(self, result=7):
        self.result = result
        self.validated = []
        self.collected = []

    def validate_source(self, source):
        self.validated.append(source)

    def collect(self, source):
        self.collected.append(source)
        return self.result

    def execute_action(self, action):
        return {}


def test_platform_and_vendor_adapters_require_and_dispatch_registered_hooks():
    hooks = VendorHookRegistry()
    hook = RecordingHook()
    hooks.register("sensor", hook)

    missing = _item(source_type="platform_api", source={})
    with pytest.raises(ValueError, match="registered hook"):
        PlatformAPIAdapter(hooks).validate(missing)

    platform_item = _item(
        source_type="platform_api", source={"hook": "sensor", "field": "value"}
    )
    platform = PlatformAPIAdapter(hooks)
    platform.validate(platform_item)
    assert platform.get_value(platform_item) == 7

    vendor_item = _item(source_type="vendor_sensor", source={"field": "value"})
    vendor = VendorAdapter("vendor_sensor", hooks)
    hooks.register("vendor_sensor", RecordingHook(8))
    vendor.validate(vendor_item)
    assert vendor.get_value(vendor_item) == 8

    explicit = _item(
        source_type="vendor_sensor",
        source={"hook": "sensor", "field": "other"},
    )
    vendor.validate(explicit)
    assert vendor.get_value(explicit) == 7
    assert hook.validated == [platform_item.source, explicit.source]
    assert hook.collected == [platform_item.source, explicit.source]
