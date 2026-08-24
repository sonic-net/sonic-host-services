from __future__ import absolute_import

from copy import deepcopy
import json
import logging

import pytest

from dldd.dse import DSERegistry

from .conftest import (
    ControlledDSEHook,
    DSE_FAULT_KEY,
    dse_integration_rule_document,
    eventually,
    integration_rule_document,
    SOURCE_KEY,
)


pytestmark = pytest.mark.dldd_integration


def test_full_service_expands_samples_and_retires_authoritative_dse_child(
    dse_integration_environment,
):
    state_db = dse_integration_environment.state_db
    hook = dse_integration_environment.source
    with dse_integration_environment.running() as running:
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
        active = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
        )
        assert active["component_name"] == "DSE_SENSOR0"
        assert json.loads(active["events"])[0]["value_read"] == "20"
        assert hook.source_calls.count("DSE_SENSOR0") >= 2
        assert hook.comparator_calls.count("DSE_SENSOR0") >= 2

        hook.remove("DSE_SENSOR0")
        inactive = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="INACTIVE"
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


def test_non_authoritative_dse_omission_keeps_child_and_active_fault(
    dse_integration_environment,
):
    state_db = dse_integration_environment.state_db
    hook = dse_integration_environment.source
    with dse_integration_environment.running() as running:
        eventually(
            lambda: running.service.orchestrator is not None
            and any(
                item.component_name == "DSE_SENSOR0"
                for item in running.service.orchestrator.work_items.values()
            )
        )
        hook.set_value("DSE_SENSOR0", 20)
        active = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
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


def test_restart_reconciles_retained_active_dse_fault_after_expansion(
    dse_integration_environment,
):
    state_db = dse_integration_environment.state_db
    hook = dse_integration_environment.source
    hook.set_value("DSE_SENSOR0", 20)
    with dse_integration_environment.running():
        before = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
        )

    with dse_integration_environment.running() as second:
        after = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY,
            predicate=lambda unused_row: (
                second.service.orchestrator is not None
                and (9900002, "DSE_SENSOR0")
                not in second.service.orchestrator.pending_dynamic_faults
            ),
            status="ACTIVE",
        )
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert after["active_rules_checksum"] == before[
            "active_rules_checksum"
        ]
        assert not after.get("reason")
        assert not second.service.orchestrator.service_diagnostics


def test_restart_authoritative_absence_refreshes_retained_inactive_dse_fault(
    dse_integration_environment,
):
    state_db = dse_integration_environment.state_db
    hook = dse_integration_environment.source
    hook.set_value("DSE_SENSOR0", 20)
    with dse_integration_environment.running():
        dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
        )
        hook.set_value("DSE_SENSOR0", 0)
        before = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="INACTIVE"
        )
        assert not before.get("reason")

    hook.remove("DSE_SENSOR0")
    with dse_integration_environment.running() as second:
        after = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY,
            predicate=lambda row: "authoritative DSE discovery"
            in row.get("reason", ""),
        )
        assert after["status"] == "INACTIVE"
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert state_db.ttls[DSE_FAULT_KEY] == 3600
        assert not second.service.orchestrator.service_diagnostics


def test_mixed_dse_and_common_redis_rule_has_only_real_scoped_instances(
    integration_environment_factory, caplog,
):
    caplog.set_level(logging.WARNING, logger="dldd.monitor")
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

    # Keep a native Redis monitor in the same generation.  This mirrors a
    # mixed production ruleset and proves that commands for the Redis-backed
    # predicates cloned by DSE expansion are not misrouted to that monitor.
    control = integration_rule_document()["signatures"][0]
    control["signature"]["metadata"].update(
        id=9900003,
        name="DLDD_DSE_COMMON_ROUTING_CONTROL",
    )
    document["signatures"].append(control)

    hook = ControlledDSEHook()
    hook.set_value("DSE_SENSOR0", 20)
    hook.set_value("DSE_SENSOR1", 20)
    hook.set_direct_row("DLDD_COMMON_SENSOR|GLOBAL", {"value": "20"})
    hook.set_direct_row(SOURCE_KEY, {"value": "5"})
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
    with environment.running() as running:
        def expanded_rule_items():
            orchestrator = running.service.orchestrator
            if orchestrator is None:
                return None
            items = tuple(
                item
                for item in orchestrator.work_items.values()
                if item.rule_id == 9900002
            )
            return items if len(items) == 4 else None

        items = eventually(expanded_rule_items)
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
        assert {
            monitor.plan.monitor_type for monitor in running.service.monitors
        } >= {"redis", "common"}
        assert dict(plan.polling_intervals) == {
            "redis": 2.0,
            "file": 3.0,
            "common": 7.0,
        }
        eventually(lambda: len(hook.direct_read_calls) >= 2)
        for component in ("DSE_SENSOR0", "DSE_SENSOR1"):
            environment.wait_for_row(
                "FAULT_INFO|{}|SYMPTOM_OVER_THRESHOLD".format(component),
                status="ACTIVE",
            )

        def scoped_common_predicates_are_released():
            items, states = plan.runtime_snapshot()
            keys = [
                key
                for key, item in items.items()
                if item.rule_id == 9900002 and item.event_id == 2
            ]
            return len(keys) == 2 and all(
                states[key].state.value == "READY" for key in keys
            )

        eventually(scoped_common_predicates_are_released)
        assert not running.service.orchestrator.service_diagnostics
        assert not any(
            "discarding command for unknown key" in record.getMessage()
            for record in caplog.records
            if record.name == "dldd.monitor"
        )


def test_empty_dse_inventory_completes_discovery_stabilization(
    dse_integration_environment,
):
    hook = dse_integration_environment.source
    hook.remove("DSE_SENSOR0")
    hook.remove("DSE_SENSOR1")
    with dse_integration_environment.running() as running:
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
