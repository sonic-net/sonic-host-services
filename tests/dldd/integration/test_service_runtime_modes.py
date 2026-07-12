from __future__ import absolute_import

from copy import deepcopy
from threading import RLock

import pytest

from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.runtime import MonitorWorkState

from .conftest import (
    FAULT_KEY,
    ControlledHashSource,
    RunningService,
    eventually,
    integration_rule_document,
)


pytestmark = pytest.mark.dldd_integration


class MaintenanceHook(VendorHook):
    def __init__(self):
        self.expected = True
        self.error = None
        self.operations = []
        self._lock = RLock()

    def collect(self, operation):
        with self._lock:
            self.operations.append(dict(operation))
            if self.error is not None:
                raise self.error
            return self.expected

    def execute_action(self, unused_action):
        return {}

    def set_expected(self, expected):
        with self._lock:
            self.expected = expected

    def fail_with(self, error):
        with self._lock:
            self.error = error

    def recover(self):
        with self._lock:
            self.error = None


def test_expected_maintenance_suspends_without_breaking_then_recovers(
    integration_environment_factory,
):
    hook = MaintenanceHook()
    hooks = VendorHookRegistry()
    hooks.register("source_lifecycle", hook)
    environment = integration_environment_factory(
        document=integration_rule_document(), vendor_hooks=hooks
    )
    source = environment["source"]
    running = RunningService(environment["service"]()).start()
    try:
        eventually(
            lambda: running.service.orchestrator is not None
            and running.service.orchestrator.service_state() == "OK"
        )
        source.fail_with(RuntimeError("planned producer restart"))
        suspended = eventually(
            lambda: next(
                iter(running.service.orchestrator.source_status.values()), None
            )
        )
        assert suspended["state"] == "SUSPENDED"
        assert suspended["graceful"] is True
        assert not running.service.orchestrator.broken_rules
        plan = running.service.monitors[0].plan
        state = next(iter(plan.state_by_key.values()))
        eventually(lambda: state.state is MonitorWorkState.SUSPENDED)
        assert not environment["state_db"].hgetall(FAULT_KEY)
        assert hook.operations[-1]["operation"] == "is_expected_maintenance"

        hook.fail_with(RuntimeError("platform lifecycle unavailable"))
        unavailable = eventually(
            lambda: (
                status
                if (
                    status := next(
                        iter(
                            running.service.orchestrator.source_status.values()
                        ),
                        None,
                    )
                )
                and status.get("state") == "UNAVAILABLE"
                else None
            )
        )
        assert unavailable["graceful"] is False
        eventually(lambda: state.state is MonitorWorkState.DEGRADED)

        hook.recover()
        hook.set_expected(False)
        source.recover()
        eventually(
            lambda: any(
                status.get("state") == "RECOVERED"
                for status in running.service.orchestrator.source_status.values()
            )
        )
        eventually(lambda: state.state is MonitorWorkState.READY)
        assert running.service.orchestrator.service_state() == "OK"
    finally:
        running.stop()


def _arbitration_document():
    document = integration_rule_document()
    high = document["signatures"][0]["signature"]
    high["metadata"].update(
        name="DLDD_ARBITRATION_HIGH",
        id=9900101,
        severity="CRITICAL",
        priority=20,
    )
    high["conditions"]["events"][0]["event"]["path"]["key"] = (
        "DLDD_TEST_SENSOR|HIGH"
    )
    low_wrapper = deepcopy(document["signatures"][0])
    low = low_wrapper["signature"]
    low["metadata"].update(
        name="DLDD_ARBITRATION_LOW",
        id=9900102,
        severity="WARNING",
        priority=1,
    )
    low["conditions"]["events"][0]["event"]["path"]["key"] = (
        "DLDD_TEST_SENSOR|LOW"
    )
    document["signatures"].append(low_wrapper)
    return document


def test_fault_arbiter_publishes_winner_then_promotes_active_alternate(
    integration_environment_factory,
):
    source = ControlledHashSource(
        "DLDD_TEST_SENSOR|HIGH", {"value": "20"}
    )
    source.set_row("DLDD_TEST_SENSOR|LOW", {"value": "20"})
    environment = integration_environment_factory(
        document=_arbitration_document(), source=source
    )
    state_db = environment["state_db"]
    running = RunningService(environment["service"]()).start()
    try:
        high = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(FAULT_KEY)).get("rule")
                == "DLDD_ARBITRATION_HIGH"
                else None
            )
        )
        assert high["status"] == "ACTIVE"
        origin = high["origin_time"]

        source.set_value(5, "DLDD_TEST_SENSOR|HIGH")
        promoted = eventually(
            lambda: (
                row
                if (row := state_db.hgetall(FAULT_KEY)).get("rule")
                == "DLDD_ARBITRATION_LOW"
                else None
            )
        )
        assert promoted["status"] == "ACTIVE"
        assert promoted["origin_time"] == origin
        assert running.service.orchestrator.published_by_key[
            ("TEST_SENSOR", "SYMPTOM_OVER_THRESHOLD")
        ] == 9900102
    finally:
        running.stop()


def test_service_restarts_one_orderly_stopped_monitor_with_same_plan(
    integration_environment_factory,
):
    environment = integration_environment_factory(
        document=integration_rule_document()
    )
    service = environment["service"]()
    original_new_monitor = service._new_monitor
    created = []

    def controlled_new_monitor(plan):
        monitor = original_new_monitor(plan)
        created.append(monitor)
        if len(created) == 1:
            # Model one monitor returning unexpectedly without corrupting its
            # immutable plan or the shared service stop token.
            monitor.run = lambda: None
        return monitor

    service._new_monitor = controlled_new_monitor
    running = RunningService(service).start()
    try:
        replacement = eventually(
            lambda: (
                service.monitors[0]
                if len(created) >= 2
                and service.monitors
                and service.monitors[0].is_alive()
                else None
            )
        )
        assert replacement.plan is created[0].plan
        assert any(
            "stopped unexpectedly and was restarted" in item.get("reason", "")
            for item in replacement.diagnostics
        )
        eventually(
            lambda: environment["state_db"]
            .hgetall("DLDD_STATUS|process_state")
            .get("state")
            == "OK"
        )
        assert running.thread.is_alive()
    finally:
        running.stop()
