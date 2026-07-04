from __future__ import absolute_import

from dataclasses import replace
from queue import Empty, Queue
import subprocess
import time

import pytest

from dldd.adapters import (
    CLIAdapter,
    FileAdapter,
    I2CAdapter,
    PlatformAPIAdapter,
    RedisAdapter,
    SysfsAdapter,
)
from dldd.evaluators import EvaluationContractError, evaluate
from dldd.monitor import MonitorThread, command_for_event
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.runtime import (
    CollectedValue,
    EvaluationResult,
    EvaluationResultType,
    MonitorCommandType,
    MonitorExecutionPlan,
    MonitorWorkItem,
    MonitorWorkState,
    MonitorWorkStateRecord,
    SourceAvailability,
    ValueConfig,
)
from dldd.validation import load_rules


class SequenceAdapter(object):
    def __init__(self, results):
        self.results = list(results)

    def collect(self, _item):
        return self.results.pop(0)


def result(kind, value=None):
    collected = None if value is None else CollectedValue(value, value)
    return EvaluationResult(
        kind,
        value=collected,
        evaluator_type="boolean",
        expected=True,
        completed_at=100.0,
    )


def work_item(key="1:1:PSU0:SYMPTOM_OVER_THRESHOLD:source"):
    return MonitorWorkItem(
        rule_id=1000001,
        rule_name="PSU_FAULT",
        rule_version="1.0.0",
        schema_version="0.0.1",
        severity="CRITICAL",
        priority=1,
        symptom="SYMPTOM_OVER_THRESHOLD",
        error_type="POWER",
        component_type="PSU",
        component_name="PSU0",
        event_id=1,
        correlation_key=key,
        source_id="test:PSU0",
        source_type="test",
        source={},
        evaluation={"type": "boolean", "value": True},
    )


def plan(item):
    return MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {item.correlation_key: item},
        {item.correlation_key: MonitorWorkStateRecord()},
        Queue(),
    )


def test_evaluators_cover_schema_contracts():
    assert evaluate({"type": "mask", "logic": "&", "value": "0b1000"}, "0b1100")
    assert evaluate({"type": "comparison", "operator": ">", "value": 3}, "4")
    assert evaluate({"type": "string", "operator": "contains", "value": "SU", "case_sensitive": False}, "psu")
    assert evaluate({"type": "boolean", "value": True}, "true")
    with pytest.raises(EvaluationContractError):
        evaluate({"type": "comparison", "operator": "bad", "value": 3}, 4)


def test_regex_evaluation_times_out_catastrophic_backtracking():
    started = time.monotonic()

    with pytest.raises(EvaluationContractError, match="exceeded"):
        evaluate(
            {"type": "string", "operator": "regex", "value": "(a+)+$"},
            "a" * 10000 + "!",
        )

    assert time.monotonic() - started < 1.0


def test_monitor_single_flight_and_clear_transition():
    item = work_item()
    evidence = Queue()
    adapter = SequenceAdapter(
        [
            result(EvaluationResultType.NO_MATCH, False),
            result(EvaluationResultType.MATCH, True),
            result(EvaluationResultType.NO_MATCH, False),
        ]
    )
    monitor = MonitorThread(plan(item), {"test": adapter}, evidence)

    monitor.poll_once()
    with pytest.raises(Empty):
        evidence.get_nowait()
    monitor.poll_once()
    matched = evidence.get_nowait()
    state = monitor.plan.state_by_key[item.correlation_key]
    assert state.state == MonitorWorkState.IN_FLIGHT

    monitor.poll_once()
    assert len(adapter.results) == 1
    monitor.plan.control_queue.put(
        command_for_event(
            matched,
            MonitorCommandType.RESUME,
            MonitorWorkState.READY,
            "processed",
        )
    )
    monitor.drain_control_queue()
    monitor.poll_once()
    cleared = evidence.get_nowait()
    assert cleared.result.result == EvaluationResultType.NO_MATCH


def test_stale_monitor_command_is_rejected():
    item = work_item()
    evidence = Queue()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([result(EvaluationResultType.MATCH, True)])},
        evidence,
    )
    monitor.poll_once()
    event = evidence.get_nowait()
    command = command_for_event(
        event,
        MonitorCommandType.RESUME,
        MonitorWorkState.READY,
        "processed",
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.work_state_generation += 1
    assert monitor.apply_command(command) is False
    assert state.state == MonitorWorkState.IN_FLIGHT


def test_execution_plan_items_are_immutable():
    item = work_item()
    execution_plan = plan(item)
    with pytest.raises(TypeError):
        execution_plan.items_by_key["new"] = item
    with pytest.raises(TypeError):
        item.source["new"] = "value"


def test_source_recovery_requires_configured_success_samples():
    item = work_item()
    evidence = Queue()
    unavailable = EvaluationResult(
        EvaluationResultType.SOURCE_UNAVAILABLE,
        completed_at=100.0,
        error="missing",
    )
    adapter = SequenceAdapter(
        [
            unavailable,
            result(EvaluationResultType.MATCH, True),
            result(EvaluationResultType.MATCH, True),
        ]
    )
    monitor = MonitorThread(
        plan(item),
        {"test": adapter},
        evidence,
        source_recovery_samples=2,
    )
    monitor.poll_once()
    unavailable_event = evidence.get_nowait()
    monitor.apply_command(
        command_for_event(
            unavailable_event,
            MonitorCommandType.RESUME,
            MonitorWorkState.DEGRADED,
            "retry",
        )
    )
    monitor.poll_once()
    with pytest.raises(Empty):
        evidence.get_nowait()
    monitor.poll_once()
    assert evidence.get_nowait().result.result == EvaluationResultType.SOURCE_RECOVERED


def test_queue_full_does_not_lose_a_clear_transition():
    item = work_item()
    state = MonitorWorkStateRecord(
        state=MonitorWorkState.READY,
        last_sample_state=EvaluationResultType.MATCH.value,
    )
    execution_plan = plan(item)
    execution_plan.state_by_key[item.correlation_key] = state
    evidence = Queue(maxsize=1)
    evidence.put(object())
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [
                    result(EvaluationResultType.NO_MATCH, False),
                    result(EvaluationResultType.NO_MATCH, False),
                ]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.last_sample_state == EvaluationResultType.MATCH.value
    assert state.state == MonitorWorkState.READY
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.NO_MATCH


def test_queue_full_does_not_lose_a_source_recovery_transition():
    item = work_item()
    state = MonitorWorkStateRecord(
        state=MonitorWorkState.DEGRADED,
        source_status=SourceAvailability.UNAVAILABLE,
    )
    execution_plan = plan(item)
    execution_plan.state_by_key[item.correlation_key] = state
    evidence = Queue(maxsize=1)
    evidence.put(object())
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [
                    result(EvaluationResultType.MATCH, True),
                    result(EvaluationResultType.MATCH, True),
                ]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.source_status == SourceAvailability.UNAVAILABLE
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.SOURCE_RECOVERED
    assert state.source_status == SourceAvailability.AVAILABLE


def test_queue_full_failure_does_not_invent_an_unobserved_recovery():
    item = work_item()
    execution_plan = plan(item)
    state = execution_plan.state_by_key[item.correlation_key]
    evidence = Queue(maxsize=1)
    evidence.put(object())
    unavailable = EvaluationResult(
        EvaluationResultType.SOURCE_UNAVAILABLE,
        completed_at=100.0,
        error="missing",
    )
    monitor = MonitorThread(
        execution_plan,
        {
            "test": SequenceAdapter(
                [unavailable, result(EvaluationResultType.MATCH, True)]
            )
        },
        evidence,
    )

    monitor.poll_once()
    assert state.source_status == SourceAvailability.AVAILABLE
    evidence.get_nowait()
    monitor.poll_once()

    assert evidence.get_nowait().result.result == EvaluationResultType.MATCH


def test_due_recheck_does_not_poll_unrelated_ready_keys_early():
    first = work_item("first")
    second = replace(work_item("second"), event_id=2)
    execution_plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "sha256:test",
        {first.correlation_key: first, second.correlation_key: second},
        {
            first.correlation_key: MonitorWorkStateRecord(
                state=MonitorWorkState.RECHECK_REQUESTED
            ),
            second.correlation_key: MonitorWorkStateRecord(),
        },
        Queue(),
    )
    calls = []

    class RecordingAdapter(object):
        def collect(self, item):
            calls.append(item.correlation_key)
            return result(EvaluationResultType.NO_MATCH, False)

    monitor = MonitorThread(
        execution_plan,
        {"test": RecordingAdapter()},
        Queue(),
        clock=lambda: 0.0,
    )
    monitor._next_poll = 60.0

    monitor.run_once()

    assert calls == ["first"]


def test_lower_dynamic_polling_interval_pulls_next_poll_forward():
    clock = [0.0]
    item = work_item()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: clock[0],
    )
    monitor._next_poll = 3600.0

    monitor.update_polling_interval(1)

    assert monitor.plan.polling_interval == 1
    assert monitor._next_poll == 1.0


def test_recheck_lease_expiry_recovers_key_and_records_diagnostic():
    item = work_item()
    monitor = MonitorThread(
        plan(item),
        {"test": SequenceAdapter([])},
        Queue(),
        clock=lambda: 100.0,
    )
    state = monitor.plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.RECHECK_REQUESTED
    state.hold_deadline = 99.0

    monitor._recover_expired_ownership(100.0)

    assert state.state == MonitorWorkState.READY
    assert monitor.diagnostics[-1]["state"] == "RECHECK_REQUESTED"


def test_static_validation_does_not_require_platform_dse_hook():
    validated = load_rules(
        "tests/dldd/fixtures/valid-psu-hld.yaml", materialize=False
    )
    assert validated.file_valid
    assert validated.ruleset is not None
    assert len(validated.ruleset.signatures) == 1


def test_redis_adapter_uses_full_key_and_slash_value_path():
    calls = []

    def reader(database, table, key):
        calls.append((database, table, key))
        return {"value": '{"output_voltage": 51.5}'}

    base = work_item()
    redis_item = MonitorWorkItem(
        rule_id=base.rule_id,
        rule_name=base.rule_name,
        rule_version=base.rule_version,
        schema_version=base.schema_version,
        severity=base.severity,
        priority=base.priority,
        symptom=base.symptom,
        error_type=base.error_type,
        component_type=base.component_type,
        component_name=base.component_name,
        event_id=base.event_id,
        correlation_key=base.correlation_key,
        source_id=base.source_id,
        source_type="redis",
        source={
            "database": "STATE_DB",
            "table": "PSU_INFO",
            "key": "PSU_INFO|PSU0",
            "path": "value/output_voltage",
        },
        evaluation={"type": "comparison", "operator": ">", "value": 50.0},
        value_config=ValueConfig(type="float", unit="volts"),
    )
    collected = RedisAdapter(reader).collect(redis_item)
    assert collected.result == EvaluationResultType.MATCH
    assert collected.value.normalized == 51.5
    assert calls == [("STATE_DB", "PSU_INFO", "PSU_INFO|PSU0")]


class CollectingHook(VendorHook):
    def collect(self, operation):
        return operation["value"]

    def execute_action(self, action):
        return {}


class ValidatingHook(CollectingHook):
    def validate_source(self, operation):
        if "value" not in operation:
            raise ValueError("platform source requires value")


class I2CResolvingHook(CollectingHook):
    def __init__(self):
        self.validated = []

    def validate_source(self, operation):
        self.validated.append(operation["bus"])

    def resolve_i2c_bus(self, bus, operation):
        return "6" if bus == "IO-MUX-6" else bus


def test_builtin_adapters_conform_without_hardware(tmp_path):
    base = work_item()
    source_file = tmp_path / "source.json"
    source_file.write_text('{"reading": 5}')
    file_item = replace(
        base,
        source_type="file",
        source={"file": str(source_file), "format": "json", "path": "reading"},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    sysfs_file = tmp_path / "sysfs"
    sysfs_file.write_text("5")
    sysfs_item = replace(
        base,
        source_type="sysfs",
        source={"file": str(sysfs_file), "format": "integer"},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    cli_item = replace(
        base,
        source_type="cli",
        source={"argv": ["diagnostic"], "timeout": 1, "max_output_bytes": 32},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    i2c_item = replace(
        base,
        source_type="i2c",
        source={
            "i2c_type": "get",
            "bus": "6",
            "chip_addr": "0x58",
            "command": "0x7a",
            "size": "b",
        },
        evaluation={"type": "mask", "logic": "&", "value": "0x80"},
    )
    hooks = VendorHookRegistry()
    hooks.register("platform", CollectingHook())
    platform_item = replace(
        base,
        source_type="platform_api",
        source={"hook": "platform", "value": 5},
        evaluation={"type": "comparison", "operator": ">", "value": 4},
    )
    adapters = (
        (FileAdapter(), file_item),
        (SysfsAdapter(), sysfs_item),
        (
            CLIAdapter(
                lambda argv, **kwargs: subprocess.CompletedProcess(
                    argv, 0, stdout=b"5", stderr=b""
                )
            ),
            cli_item,
        ),
        (I2CAdapter(lambda source: "0x80"), i2c_item),
        (PlatformAPIAdapter(hooks), platform_item),
    )

    for adapter, item in adapters:
        adapter.validate(item)
        assert adapter.collect(item).result == EvaluationResultType.MATCH


def test_file_adapter_rejects_unsupported_format_before_polling(tmp_path):
    item = replace(
        work_item(),
        source_type="file",
        source={"file": str(tmp_path / "source"), "format": "pickle"},
    )

    with pytest.raises(ValueError, match="unsupported file format"):
        FileAdapter().validate(item)


def test_platform_hook_preflights_vendor_source_fields():
    hooks = VendorHookRegistry()
    hooks.register("platform", ValidatingHook())
    item = replace(
        work_item(),
        source_type="platform_api",
        source={"hook": "platform"},
    )

    with pytest.raises(ValueError, match="requires value"):
        PlatformAPIAdapter(hooks).validate(item)


def test_i2c_adapter_vendor_hook_resolves_logical_bus_before_collection():
    observed = []
    hook = I2CResolvingHook()
    hooks = VendorHookRegistry()
    hooks.register("i2c", hook)
    item = replace(
        work_item(),
        source_type="i2c",
        source={
            "i2c_type": "get",
            "bus": "IO-MUX-6",
            "chip_addr": "0x58",
            "command": "0x7a",
            "size": "b",
        },
        evaluation={"type": "mask", "logic": "&", "value": "0x80"},
    )
    adapter = I2CAdapter(
        lambda source: observed.append(source["bus"]) or "0x80",
        hooks=hooks,
    )

    adapter.validate(item)
    result = adapter.collect(item)

    assert result.result == EvaluationResultType.MATCH
    assert hook.validated == ["IO-MUX-6"]
    assert observed == ["6"]
