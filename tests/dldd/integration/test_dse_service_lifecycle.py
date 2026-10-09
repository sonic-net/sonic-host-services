from __future__ import absolute_import

import json

import pytest

from .conftest import DSE_FAULT_KEY, eventually


pytestmark = pytest.mark.dldd_integration


def test_full_service_expands_samples_and_retires_missing_dse_child(
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
        assert not state_db.hgetall(DSE_FAULT_KEY)

        hook.set_value("DSE_SENSOR0", 20)
        active = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
        )
        assert json.loads(active["events"])[0]["value_read"] == "20"

        hook.remove("DSE_SENSOR0")
        inactive = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="INACTIVE"
        )
        assert inactive["origin_time"] == active["origin_time"]
        assert "DSE discovery" in inactive["reason"]
        assert state_db.ttls[DSE_FAULT_KEY] == 3600
        eventually(
            lambda: all(
                item.component_name != "DSE_SENSOR0"
                for item in running.service.orchestrator.work_items.values()
            )
        )


def test_restart_reconciles_an_active_dse_fault_after_expansion(
    dse_integration_environment,
):
    hook = dse_integration_environment.source
    hook.set_value("DSE_SENSOR0", 20)
    with dse_integration_environment.running():
        before = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE"
        )

    with dse_integration_environment.running() as restarted:
        after = dse_integration_environment.wait_for_row(
            DSE_FAULT_KEY,
            predicate=lambda unused_row: (
                restarted.service.orchestrator is not None
                and (9900002, "DSE_SENSOR0")
                not in restarted.service.orchestrator.pending_dynamic_faults
            ),
            status="ACTIVE",
        )
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert after["active_rules_checksum"] == before[
            "active_rules_checksum"
        ]
        assert not after.get("reason")
