from __future__ import absolute_import

import json
from pathlib import Path
import time

import pytest

from dldd.service import TelemetryUnavailable
from .conftest import FAULT_KEY, eventually


pytestmark = pytest.mark.dldd_integration


def test_full_service_detects_fake_fault_clears_and_stops_cleanly(
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
        assert active["rule"] == "DLDD_INTEGRATION_THRESHOLD"
        assert active["component_type"] == "TEST_SENSOR"
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


def test_full_service_survives_source_read_error_and_recovers(
    integration_environment,
):
    source = integration_environment.source
    with integration_environment.running() as running:
        integration_environment.wait_for_status("OK")
        source.fail_with(RuntimeError("synthetic STATE_DB read failure"))
        unavailable = eventually(
            lambda: (
                status
                if running.service.orchestrator is not None
                and (
                    status := next(
                        iter(running.service.orchestrator.source_status.values()),
                        None,
                    )
                )
                is not None
                else None
            )
        )
        assert unavailable["state"] == "UNAVAILABLE"
        assert "synthetic STATE_DB read failure" in unavailable["reason"]
        assert running.service._publish_status() is True
        degraded = integration_environment.row("DLDD_STATUS|process_state")
        assert degraded["state"] == "DEGRADED"
        source_status = json.loads(degraded["source_status"])
        assert source_status[0]["state"] == "UNAVAILABLE"
        assert not integration_environment.row(FAULT_KEY)

        source.recover()
        eventually(
            lambda: (
                running.service.orchestrator is not None
                and not running.service.orchestrator.broken_rules
                and any(
                    item.get("state") == "RECOVERED"
                    for item in running.service.orchestrator.source_status.values()
                )
            )
        )
        assert running.service._publish_status() is True
        assert integration_environment.row("DLDD_STATUS|process_state")[
            "state"
        ] == "OK"


def test_full_service_recovers_from_transient_database_read_error(
    integration_environment,
):
    state_db = integration_environment.state_db
    state_db.fail_reads_with(RuntimeError("synthetic STATE_DB read failure"))
    with integration_environment.running() as running:
        eventually(lambda: state_db.read_failures > 0)
        state_db.clear_failures()
        eventually(
            lambda: integration_environment.row(
                "DLDD_STATUS|process_state"
            ).get("state") == "OK"
            and bool(integration_environment.row("DLDD_RULE_STATUS|active"))
        )
        assert running.thread.is_alive()
        assert running.error is None


def test_full_service_stops_uncleanly_after_persistent_database_write_error(
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


def test_full_service_stops_before_monitors_after_persistent_fault_scan_error(
    integration_environment,
):
    state_db = integration_environment.state_db
    state_db.fail_reads_with(RuntimeError("persistent fault scan failure"))
    running = integration_environment.running().start()

    error = running.wait_stopped(timeout=8)

    assert isinstance(error, TelemetryUnavailable)
    assert "fault reconciliation failed 3 consecutive times" in str(error)
    assert not running.service.monitors
    assert running.service.orchestrator is not None


def test_full_service_restart_reconciles_existing_active_fault(
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
            ).get("state") == "OK",
            status="ACTIVE",
        )
        assert after["origin_time"] == before["origin_time"]
        assert after["occurrences"] == before["occurrences"]
        assert after["active_rules_checksum"] == before["active_rules_checksum"]
