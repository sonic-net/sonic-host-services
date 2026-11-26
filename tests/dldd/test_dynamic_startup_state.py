"""Startup BROKEN seeds apply only to the child's first runtime lifetime."""

from dataclasses import replace
from queue import Queue

from dldd.adapters import DSEAdapter
from dldd.config import DLDDConfig
from dldd.correlation import CorrelationEngine
from dldd.dse import (
    DSEBinding, DSEContext, DSEExpansionResult, DSEReference, DSESourceHandle,
)
from dldd.monitor import MonitorThread
from dldd.orchestrator import PrimaryOrchestrator
from dldd.planner import work_items_for_dse_expansion
from dldd.runtime import MonitorExecutionPlan, MonitorWorkState
from dldd.telemetry import TelemetryPublisher
from tests.dldd.test_orchestrator import dse_retirement_fixture
from tests.dldd_fakes import FakeStateDB


def test_materialized_broken_seed_is_consumed_despite_failed_sibling_inventory():
    unused_primary, unused_db, source_plan, base, unused_signature, unused_fault = (
        dse_retirement_fixture(template_ids=("present", "failed"), runtime_item=False)
    )
    visible = [True]
    collections: list[str] = []
    binding = DSEBinding(instance="DYNAMIC0", source_id="SENSOR_INFO|DYNAMIC0")

    def expand(context):
        if context.event_id == 2:
            raise RuntimeError("sibling initial inventory unavailable")
        return DSEExpansionResult((binding,) if visible[0] else ())

    def collect(invocation):
        collections.append(invocation.binding.instance)
        return 10.0

    handle = DSESourceHandle(
        DSEReference("sensor*", "get_value"),
        expand,
        collect,
    )
    templates = {
        name: replace(
            template,
            item=replace(
                template.item,
                dse_binding=None,
                dse_source_handle=handle,
                dse_context=DSEContext(event_id=index),
            ),
            source_handle=handle,
        )
        for index, (name, template) in enumerate(
            (("present", source_plan.templates_by_key["present"]),
             ("failed", source_plan.templates_by_key["failed"])),
            start=1,
        )
    }
    plan = MonitorExecutionPlan(
        "common", "common", 60, "sha256:test", {}, {}, Queue(),
        templates_by_key=templates,
    )
    config = DLDDConfig()
    primary = PrimaryOrchestrator(
        Queue(), {"common": plan}, {}, CorrelationEngine({}),
        TelemetryPublisher(FakeStateDB(), config), config, "sha256:test",
    )
    evidence = primary.evidence_queue
    child = work_items_for_dse_expansion(
        templates["present"], DSEExpansionResult((binding,))
    )[0]
    key = child.correlation_key
    absent_key = key + ":never-materialized"
    for restored_key in (key, absent_key):
        primary.restore_broken_work(
            restored_key,
            {"rule_id": base.rule_id, "correlation_key": restored_key,
             "state": "BROKEN", "reason": "persisted comparator failure"},
        )
    adapter = DSEAdapter()
    monitor = MonitorThread(plan, {"dse": adapter}, evidence)

    def inventory(name, now):
        monitor._expand_template(
            name, templates[name], plan.expansion_state_by_key[name], adapter, now
        )
        if not evidence.empty():
            primary.process_expansion(evidence.get_nowait())
            evidence.task_done()
            monitor.drain_control_queue()

    inventory("failed", 0)
    assert plan.expansion_state_by_key["failed"].binding_fingerprint is None
    assert "sibling initial inventory" in (
        plan.expansion_state_by_key["failed"].last_error
    )
    inventory("present", 0)
    assert plan.state_by_key[key].state == MonitorWorkState.BROKEN
    monitor.poll_once(now=0)
    assert collections == []

    visible[0] = False
    inventory("present", 1)
    assert key not in plan.state_by_key
    assert key not in primary.broken_rules
    assert absent_key in primary.broken_rules

    visible[0] = True
    inventory("present", 2)
    assert key not in primary.broken_rules
    assert plan.state_by_key[key].state == MonitorWorkState.READY
    assert plan.initial_broken_keys == frozenset(((base.rule_id, absent_key),))
    monitor.poll_once(now=2)
    assert collections == ["DYNAMIC0"]
    assert plan.expansion_state_by_key["failed"].binding_fingerprint is None
