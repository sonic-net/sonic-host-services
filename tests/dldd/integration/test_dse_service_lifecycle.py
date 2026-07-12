from __future__ import absolute_import

from copy import deepcopy
import json

import pytest

from dldd.dse import DSERegistry

from .conftest import (
    ControlledDSEHook,
    DSE_FAULT_KEY,
    RunningService,
    dse_integration_rule_document,
    eventually,
)


pytestmark = pytest.mark.dldd_integration


def test_full_service_expands_samples_and_retires_authoritative_dse_child(
    dse_integration_environment,
):
    state_db = dse_integration_environment["state_db"]
    hook = dse_integration_environment["source"]
    running = RunningService(dse_integration_environment["service"]()).start()
    try:
        eventually(
            lambda: running.service.orchestrator is not None
            and {
                item.component_name
                for item in running.service.orchestrator.work_items.values()
            }
            == {"DSE_SENSOR0", "DSE_SENSOR1"}
        )
        eventually(
            lambda: set(hook.source_calls) == {"DSE_SENSOR0", "DSE_SENSOR1"}
            and set(hook.comparator_calls)
            == {"DSE_SENSOR0", "DSE_SENSOR1"}
        )
        assert hook.expansion_calls >= 1
        assert not state_db.hgetall(DSE_FAULT_KEY)

        hook.set_value("DSE_SENSOR0", 20)
        active = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "ACTIVE"
                else None
            )
        )
        assert active["component_name"] == "DSE_SENSOR0"
        assert json.loads(active["events"])[0]["value_read"] == "20"
        assert hook.source_calls.count("DSE_SENSOR0") >= 2
        assert hook.comparator_calls.count("DSE_SENSOR0") >= 2

        hook.remove("DSE_SENSOR0")
        inactive = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "INACTIVE"
                else None
            )
        )
        assert inactive["origin_time"] == active["origin_time"]
        assert json.loads(inactive["repair_actions"]) == []
        assert inactive["reason"]
        assert "authoritative DSE discovery" in inactive["reason"]
        assert state_db.ttls[DSE_FAULT_KEY] == 3600
        eventually(
            lambda: all(
                item.component_name != "DSE_SENSOR0"
                for item in running.service.orchestrator.work_items.values()
            )
        )
        assert not running.service.orchestrator.service_diagnostics
    finally:
        running.stop()


def test_non_authoritative_dse_omission_keeps_child_and_active_fault(
    dse_integration_environment,
):
    state_db = dse_integration_environment["state_db"]
    hook = dse_integration_environment["source"]
    running = RunningService(dse_integration_environment["service"]()).start()
    try:
        eventually(
            lambda: running.service.orchestrator is not None
            and any(
                item.component_name == "DSE_SENSOR0"
                for item in running.service.orchestrator.work_items.values()
            )
        )
        hook.set_value("DSE_SENSOR0", 20)
        active = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "ACTIVE"
                else None
            )
        )
        expansions_before = hook.expansion_calls
        source_reads_before = hook.source_calls.count("DSE_SENSOR0")

        hook.set_authoritative(False)
        hook.omit("DSE_SENSOR0")
        eventually(lambda: hook.expansion_calls >= expansions_before + 2)
        eventually(
            lambda: hook.source_calls.count("DSE_SENSOR0")
            > source_reads_before
        )

        assert any(
            item.component_name == "DSE_SENSOR0"
            for item in running.service.orchestrator.work_items.values()
        )
        retained = state_db.hgetall(DSE_FAULT_KEY)
        assert retained["status"] == "ACTIVE"
        assert retained["origin_time"] == active["origin_time"]
        assert not retained.get("reason")
    finally:
        running.stop()


def test_restart_reconciles_retained_active_dse_fault_after_expansion(
    dse_integration_environment,
):
    state_db = dse_integration_environment["state_db"]
    hook = dse_integration_environment["source"]
    hook.set_value("DSE_SENSOR0", 20)
    first = RunningService(dse_integration_environment["service"]()).start()
    try:
        before = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "ACTIVE"
                else None
            )
        )
    finally:
        first.stop()

    second = RunningService(dse_integration_environment["service"]()).start()
    try:
        after = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "ACTIVE"
                and second.service.orchestrator is not None
                and (9900002, "DSE_SENSOR0")
                not in second.service.orchestrator.pending_dynamic_faults
                else None
            )
        )
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert after["active_rules_checksum"] == before[
            "active_rules_checksum"
        ]
        assert not after.get("reason")
        assert not second.service.orchestrator.service_diagnostics
    finally:
        second.stop()


def test_restart_authoritative_absence_refreshes_retained_inactive_dse_fault(
    dse_integration_environment,
):
    state_db = dse_integration_environment["state_db"]
    hook = dse_integration_environment["source"]
    hook.set_value("DSE_SENSOR0", 20)
    first = RunningService(dse_integration_environment["service"]()).start()
    try:
        eventually(
            lambda: state_db.hgetall(DSE_FAULT_KEY).get("status")
            == "ACTIVE"
        )
        hook.set_value("DSE_SENSOR0", 0)
        before = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(DSE_FAULT_KEY)).get("status")
                == "INACTIVE"
                else None
            )
        )
        assert not before.get("reason")
    finally:
        first.stop()

    hook.remove("DSE_SENSOR0")
    second = RunningService(dse_integration_environment["service"]()).start()
    try:
        after = eventually(
            lambda: (
                row
                if "authoritative DSE discovery"
                in (row := state_db.hgetall(DSE_FAULT_KEY)).get("reason", "")
                else None
            )
        )
        assert after["status"] == "INACTIVE"
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert state_db.ttls[DSE_FAULT_KEY] == 3600
        assert not second.service.orchestrator.service_diagnostics
    finally:
        second.stop()


def test_mixed_dse_and_common_redis_rule_has_only_real_scoped_instances(
    integration_environment_factory,
):
    document = dse_integration_rule_document()
    signature = document["signatures"][0]["signature"]
    conditions = signature["conditions"]
    dse_event = conditions["events"][0]["event"]
    dse_event.pop("sampling_interval", None)
    direct_wrapper = deepcopy(conditions["events"][0])
    direct = direct_wrapper["event"]
    direct.update(
        id=2,
        type="redis",
        path={
            "database": "STATE_DB",
            "table": "DLDD_COMMON_SENSOR",
            "key": "DLDD_COMMON_SENSOR|GLOBAL",
            "path": "value",
        },
        evaluation={
            "type": "comparison",
            "operator": ">",
            "value": 10.0,
            "value_configs": {"type": "float", "unit": "N/A"},
        },
    )
    direct.pop("sampling_interval", None)
    conditions["events"].append(direct_wrapper)
    conditions["logic"] = "1 AND 2"

    hook = ControlledDSEHook()
    hook.set_value("DSE_SENSOR0", 20)
    hook.set_value("DSE_SENSOR1", 20)
    hook.set_direct_row("DLDD_COMMON_SENSOR|GLOBAL", {"value": "20"})
    environment = integration_environment_factory(
        document=document,
        source=hook,
        dse_registry=DSERegistry(hook=hook),
        config_values={
            "redis_monitor_polling_interval": "2",
            "file_monitor_polling_interval": "3",
            "common_monitor_polling_interval": "7",
            "source_unavailable_grace_period": "0",
        },
    )
    running = RunningService(environment["service"]()).start()
    try:
        items = eventually(
            lambda: (
                tuple(running.service.orchestrator.work_items.values())
                if running.service.orchestrator is not None
                and len(running.service.orchestrator.work_items) == 4
                else None
            )
        )
        assert {item.component_name for item in items} == {
            "DSE_SENSOR0",
            "DSE_SENSOR1",
        }
        assert {
            (item.component_name, item.event_id) for item in items
        } == {
            ("DSE_SENSOR0", 1),
            ("DSE_SENSOR0", 2),
            ("DSE_SENSOR1", 1),
            ("DSE_SENSOR1", 2),
        }
        assert all(not item.sampling_interval_is_explicit for item in items)
        plan = next(
            monitor.plan
            for monitor in running.service.monitors
            if monitor.plan.monitor_type == "common"
        )
        assert dict(plan.polling_intervals) == {
            "redis": 2.0,
            "file": 3.0,
            "common": 7.0,
        }
        eventually(lambda: len(hook.direct_read_calls) >= 2)
        for component in ("DSE_SENSOR0", "DSE_SENSOR1"):
            eventually(
                lambda component=component: environment["state_db"]
                .hgetall(
                    "FAULT_INFO|{}|SYMPTOM_OVER_THRESHOLD".format(component)
                )
                .get("status")
                == "ACTIVE"
            )
    finally:
        running.stop()


def test_empty_dse_inventory_completes_discovery_stabilization(
    dse_integration_environment,
):
    hook = dse_integration_environment["source"]
    hook.remove("DSE_SENSOR0")
    hook.remove("DSE_SENSOR1")
    running = RunningService(dse_integration_environment["service"]()).start()
    try:
        state = eventually(
            lambda: (
                next(
                    iter(
                        running.service.monitors[
                            0
                        ].plan.expansion_state_by_key.values()
                    )
                )
                if running.service.monitors
                else None
            )
        )
        eventually(lambda: state.phase == "STABLE")

        assert state.child_keys == set()
        assert state.bootstrap_scans_completed == 1
        assert state.warmup_cycles_completed == 1
        assert hook.expansion_calls >= 2
        assert running.service.orchestrator.work_items == {}
    finally:
        running.stop()
