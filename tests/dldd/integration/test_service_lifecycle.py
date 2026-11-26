from __future__ import absolute_import

import json
from pathlib import Path
import time

import pytest

from dldd.service import TelemetryUnavailable
from .conftest import FAULT_KEY, eventually


pytestmark = pytest.mark.dldd_integration


def test_full_service_detects_fault_clears_and_stops_cleanly(
    integration_environment,
):
    source = integration_environment.source
    with integration_environment.running():
        integration_environment.wait_for_status("OK")
        assert not integration_environment.row(FAULT_KEY)

        source.set_value(20)
        active = integration_environment.wait_for_row(
            FAULT_KEY, status="ACTIVE"
        )
        assert active["producer"] == "dldd"
        assert active["component_name"] == "TEST_SENSOR"
        assert json.loads(active["events"])[0]["value_read"] == "20"

        source.set_value(5)
        inactive = integration_environment.wait_for_row(
            FAULT_KEY, status="INACTIVE"
        )
        assert inactive["origin_time"] == active["origin_time"]
        assert inactive["occurrences"] == active["occurrences"]

    persisted = json.loads(
        Path(integration_environment.paths.state_file).read_text(
            encoding="utf-8"
        )
    )
    assert persisted["clean_shutdown"] is True


def test_full_service_recovers_after_a_source_read_error(
    integration_environment,
):
    source = integration_environment.source
    with integration_environment.running() as running:
        integration_environment.wait_for_status("OK")
        source.fail_with(RuntimeError("synthetic STATE_DB read failure"))
        unavailable = eventually(
            lambda: (
                next(
                    iter(running.service.orchestrator.source_status.values()),
                    None,
                )
                if running.service.orchestrator is not None
                else None
            )
        )
        assert unavailable["state"] == "UNAVAILABLE"
        assert running.service._publish_status() is True
        assert integration_environment.wait_for_status("DEGRADED")
        assert not integration_environment.row(FAULT_KEY)

        source.recover()
        eventually(
            lambda: (
                running.service.orchestrator is not None
                and not running.service.orchestrator.broken_rules
                and any(
                    item.get("state") == "RECOVERED"
                    for item in (
                        running.service.orchestrator.source_status.values()
                    )
                )
            )
        )
        assert running.service._publish_status() is True
        assert integration_environment.wait_for_status("OK")


def test_full_service_retries_publication_then_stops_uncleanly(
    integration_environment,
):
    state_db = integration_environment.state_db
    state_db.fail_writes_with(RuntimeError("synthetic STATE_DB write failure"))
    service = integration_environment.new_service()
    original_publish = service._publish_status
    attempts = []

    def publish_status():
        attempts.append(time.monotonic())
        return original_publish()

    service._publish_status = publish_status
    running = integration_environment.running(service).start()

    error = running.wait_stopped(timeout=8)

    assert isinstance(error, TelemetryUnavailable)
    assert len(attempts) == 3
    assert all(
        later - earlier >= 0.9
        for earlier, later in zip(attempts, attempts[1:])
    )
    persisted = json.loads(
        Path(integration_environment.paths.state_file).read_text(
            encoding="utf-8"
        )
    )
    assert persisted["clean_shutdown"] is False


def test_service_restarts_an_exited_monitor_with_the_same_plan(
    integration_environment,
):
    service = integration_environment.new_service()
    original_new_monitor = service._new_monitor
    created = []

    def controlled_new_monitor(plan):
        monitor = original_new_monitor(plan)
        created.append(monitor)
        if len(created) == 1:
            monitor.run = lambda: None
        return monitor

    service._new_monitor = controlled_new_monitor
    with integration_environment.running(service) as running:
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
        assert replacement.is_alive()
        assert any(
            "stopped unexpectedly and was restarted"
            in diagnostic.get("reason", "")
            for diagnostic in replacement.diagnostics
        )
        assert integration_environment.wait_for_status("OK")
        assert running.thread.is_alive()


def test_full_service_restart_reconciles_an_existing_active_fault(
    integration_environment,
):
    state_db = integration_environment.state_db
    source = integration_environment.source
    source.set_value(20)
    with integration_environment.running():
        before = integration_environment.wait_for_row(
            FAULT_KEY, status="ACTIVE"
        )

    state_db.fail_reads_with(RuntimeError("restart fault scan unavailable"))
    with integration_environment.running() as second:
        eventually(lambda: state_db.read_failures > 0)
        assert not second.service.monitors
        state_db.clear_failures()
        after = integration_environment.wait_for_row(
            FAULT_KEY,
            predicate=lambda unused_row: integration_environment.row(
                "DLDD_STATUS|process_state"
            ).get("state")
            == "OK",
            status="ACTIVE",
        )
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert after["active_rules_checksum"] == before[
            "active_rules_checksum"
        ]
