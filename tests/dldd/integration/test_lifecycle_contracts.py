from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from dldd.reset import clear_runtime_state

from .conftest import (
    CONFIG_VALUES,
    FAULT_KEY,
    RunningService,
    eventually,
    integration_rule_document,
)


pytestmark = pytest.mark.dldd_integration


def _row(database, key):
    return database.hgetall(key)


def test_no_rules_source_stops_service_cleanly_without_fatal_status(
    integration_environment_factory,
):
    environment = integration_environment_factory(document=None)
    running = RunningService(environment["service"]()).start()

    error = running.wait_stopped()

    assert error is None
    assert running.service.stop_event.is_set()
    assert running.service.activation is None
    assert running.service.orchestrator is None
    assert not _row(environment["state_db"], "DLDD_STATUS|process_state")
    assert not Path(environment["paths"].state_file).exists()


def test_present_invalid_candidate_without_fallback_stays_fatal(
    integration_environment_factory,
):
    invalid = deepcopy(integration_rule_document())
    del invalid["signatures"][0]["signature"]["metadata"]["severity"]
    environment = integration_environment_factory(document=invalid)
    running = RunningService(environment["service"]()).start()
    try:
        status = eventually(
            lambda: (
                row
                if (row := _row(
                    environment["state_db"], "DLDD_STATUS|process_state"
                )).get("state")
                == "BROKEN|FATAL"
                else None
            )
        )

        assert running.thread.is_alive()
        assert running.service.activation is None
        assert "no candidate produced a usable rules generation" in (
            running.service.fatal_reason
        )
        assert status["reason"] == running.service.fatal_reason

        manifest = json.loads(
            Path(environment["paths"].manifest).read_text(encoding="utf-8")
        )
        assert manifest["last_failure"]["errors"]
        assert "packaged candidate rejected" in manifest["last_failure"][
            "errors"
        ][0]
    finally:
        running.stop()


def test_invalid_packaged_candidate_is_recorded_and_valid_active_fallback_runs(
    integration_environment_factory,
):
    invalid = deepcopy(integration_rule_document())
    del invalid["signatures"][0]["signature"]["metadata"]["severity"]
    environment = integration_environment_factory(document=invalid)
    paths = environment["paths"]
    active = Path(paths.active)
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        yaml.safe_dump(integration_rule_document(), sort_keys=False),
        encoding="utf-8",
    )

    running = RunningService(environment["service"]()).start()
    try:
        status = eventually(
            lambda: (
                row
                if (row := _row(
                    environment["state_db"], "DLDD_STATUS|process_state"
                )).get("state")
                == "OK"
                else None
            )
        )
        assert running.service.activation.source == "active"
        assert running.service.activation.fallback_used is True
        assert status["active_rules_source"] == "active"
        assert status["activation_fallback_used"] == "true"
        assert status["activation_result"] == "PASSED"

        manifest = json.loads(Path(paths.manifest).read_text(encoding="utf-8"))
        packaged, activated = manifest["activation_attempts"][-2:]
        assert packaged["source"] == "packaged"
        assert packaged["activation_result"] == "REJECTED"
        assert packaged["usable_rule_count"] == 0
        assert any(
            "zero usable rules" in reason
            for reason in packaged["errors"]
        )
        assert activated["source"] == "active"
        assert activated["activation_result"] == "ACTIVATED"
        assert activated["fallback_used"] is True
    finally:
        running.stop()


def test_mixed_valid_and_broken_rules_activate_valid_work_and_localize_failure(
    integration_environment_factory,
):
    document = integration_rule_document()
    broken = deepcopy(document["signatures"][0])
    broken_signature = broken["signature"]
    broken_signature["metadata"].update(
        name="DLDD_INTEGRATION_BROKEN",
        id=9900099,
    )
    del broken_signature["metadata"]["severity"]
    document["signatures"].append(broken)

    environment = integration_environment_factory(document=document)
    state_db = environment["state_db"]
    source = environment["source"]
    running = RunningService(environment["service"]()).start()
    try:
        status = eventually(
            lambda: (
                row
                if (row := _row(state_db, "DLDD_STATUS|process_state")).get(
                    "state"
                )
                == "DEGRADED"
                else None
            )
        )

        activation = running.service.activation
        assert activation is not None
        assert activation.validation_result == "DEGRADED"
        assert activation.fallback_used is False
        assert len(activation.payload.materialized_rules) == 1
        assert activation.payload.materialized_rules[0].metadata.name == (
            "DLDD_INTEGRATION_THRESHOLD"
        )
        assert len(activation.broken_rules) == 1
        broken_record = activation.broken_rules[0]
        assert broken_record["rule"] == "DLDD_INTEGRATION_BROKEN"
        assert broken_record["rule_id"] == 9900099
        assert broken_record["state"] == "BROKEN"
        assert "metadata.severity" in broken_record["reason"]
        assert "missing_field" in broken_record["reason"]

        published_broken = json.loads(status["broken_rules"])
        assert published_broken == [broken_record]
        assert running.thread.is_alive()
        assert running.error is None

        source.set_value(20)
        active = eventually(
            lambda: (
                row
                if (row := _row(state_db, FAULT_KEY)).get("status")
                == "ACTIVE"
                else None
            )
        )
        assert active["rule"] == "DLDD_INTEGRATION_THRESHOLD"
        assert active["rule_id"] == "9900001"
        assert state_db.keys("FAULT_INFO|*") == [FAULT_KEY]

        assert running.service._publish_status() is True
        valid_status = _row(
            state_db,
            "DLDD_RULE_STATUS|rule|DLDD_INTEGRATION_THRESHOLD",
        )
        broken_status = _row(
            state_db,
            "DLDD_RULE_STATUS|rule|DLDD_INTEGRATION_BROKEN",
        )
        assert valid_status["health"] == "OK"
        assert valid_status["work_items_total"] == "1"
        assert valid_status["active_faults"] == "1"
        assert broken_status["health"] == "BROKEN"
        assert broken_status["work_items_total"] == "0"
        assert "metadata.severity" in broken_status["reason"]
        assert "missing_field" in broken_status["reason"]
    finally:
        running.stop()


@pytest.mark.parametrize(
    "stale_field,stale_value",
    (
        ("active_rules_checksum", "sha256:stale-generation"),
        ("schema_version", "test-stale-schema"),
    ),
    ids=("generation", "schema"),
)
def test_restart_retires_stale_active_fault_as_retained_inactive_row(
    integration_environment,
    stale_field,
    stale_value,
):
    state_db = integration_environment["state_db"]
    source = integration_environment["source"]
    source.set_value(20)
    first = RunningService(integration_environment["service"]()).start()
    try:
        original = eventually(
            lambda: (
                row
                if (row := _row(state_db, FAULT_KEY)).get("status")
                == "ACTIVE"
                else None
            )
        )
    finally:
        first.stop()

    state_db.hset(FAULT_KEY, {stale_field: stale_value})
    source.set_value(5)
    second = RunningService(integration_environment["service"]()).start()
    try:
        retired = eventually(
            lambda: (
                row
                if (row := _row(state_db, FAULT_KEY)).get("status")
                == "INACTIVE"
                and row.get("reason")
                == "stale rule/source after DLDD restart"
                else None
            )
        )
        assert retired[stale_field] == stale_value
        assert retired["origin_time"] == original["origin_time"]
        assert retired["occurrences"] == original["occurrences"]
        assert json.loads(retired["repair_actions"]) == []
        assert state_db.ttls[FAULT_KEY] == 3600
    finally:
        second.stop()


def test_default_and_full_reset_preserve_foreign_fault_ownership(
    integration_environment_factory,
):
    environment = integration_environment_factory(
        document=integration_rule_document()
    )
    state_db = environment["state_db"]
    source = environment["source"]
    source.set_value(20)
    running = RunningService(environment["service"]()).start()
    try:
        eventually(
            lambda: _row(state_db, FAULT_KEY).get("status") == "ACTIVE"
        )
    finally:
        running.stop()

    foreign_key = "FAULT_INFO|FOREIGN|SYMPTOM_UNKNOWN"
    state_db.hset(
        foreign_key,
        {
            "producer": "another-service",
            "status": "ACTIVE",
            "component_type": "FOREIGN",
            "component_name": "FOREIGN",
        },
    )
    paths = environment["paths"]
    assert Path(paths.state_file).exists()
    assert _row(state_db, "DLDD_STATUS|process_state")
    assert _row(state_db, "DLDD_RULE_STATUS|active")

    default_result = clear_runtime_state(state_db, paths.state_file)

    assert default_result.faults == 0
    assert default_result.local_state_removed is True
    assert _row(state_db, FAULT_KEY)["producer"] == "dldd"
    assert _row(state_db, foreign_key)["producer"] == "another-service"
    assert not _row(state_db, "DLDD_STATUS|process_state")
    assert not _row(state_db, "DLDD_RULE_STATUS|active")
    assert Path(paths.packaged).exists()

    Path(paths.state_file).write_text("{}", encoding="utf-8")
    artifact_directory = Path(paths.state_file).parent / "artifacts"
    artifact_directory.mkdir()
    owned_artifact = artifact_directory / "dldd-integration.tar.gz"
    owned_artifact.write_text("diagnostics", encoding="utf-8")
    foreign_artifact = artifact_directory / "foreign.tar.gz"
    foreign_artifact.write_text("foreign", encoding="utf-8")

    full_result = clear_runtime_state(
        state_db,
        paths.state_file,
        include_faults=True,
        include_artifacts=True,
        artifact_directory=str(artifact_directory),
    )

    assert full_result.faults == 1
    assert full_result.artifacts == 1
    assert full_result.local_state_removed is True
    assert not _row(state_db, FAULT_KEY)
    assert _row(state_db, foreign_key)["producer"] == "another-service"
    assert not owned_artifact.exists()
    assert foreign_artifact.exists()
    assert Path(paths.packaged).exists()


def test_retryable_source_failure_degrades_then_breaks_and_crosses_fatal_limit(
    integration_environment_factory,
):
    config = dict(CONFIG_VALUES)
    config.update(
        individual_max_failure_threshold="1",
        broken_rules_max_threshold="0",
    )
    environment = integration_environment_factory(
        document=integration_rule_document(), config_values=config
    )
    state_db = environment["state_db"]
    source = environment["source"]
    running = RunningService(environment["service"]()).start()
    try:
        eventually(
            lambda: _row(state_db, "DLDD_STATUS|process_state").get("state")
            == "OK"
        )
        source.fail_with(RuntimeError("synthetic retryable source failure"))

        degraded = eventually(
            lambda: (
                next(
                    (
                        row
                        for row in running.service.orchestrator.broken_rules.values()
                        if row.get("state") == "DEGRADED"
                    ),
                    None,
                )
                if running.service.orchestrator is not None
                else None
            )
        )
        assert degraded["failure_count"] == 1
        assert running.service.orchestrator.service_state() == "DEGRADED"
        assert running.service._publish_status() is True
        assert _row(state_db, "DLDD_STATUS|process_state")["state"] == (
            "DEGRADED"
        )

        broken = eventually(
            lambda: (
                next(
                    (
                        row
                        for row in running.service.orchestrator.broken_rules.values()
                        if row.get("state") == "BROKEN"
                    ),
                    None,
                )
                if running.service.orchestrator is not None
                else None
            )
        )
        assert broken["failure_count"] == 2
        assert "synthetic retryable source failure" in broken["reason"]
        assert running.service.orchestrator.service_state() == "BROKEN|FATAL"
        assert running.service._publish_status() is True
        status = _row(state_db, "DLDD_STATUS|process_state")
        assert status["state"] == "BROKEN|FATAL"
        assert json.loads(status["broken_rules"])[0]["state"] == "BROKEN"
    finally:
        running.stop()
