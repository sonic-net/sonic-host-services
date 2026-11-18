from __future__ import absolute_import

import pytest

from dldd.ownership import is_dldd_fault_payload
from dldd.reset import clear_runtime_state
from dldd.telemetry import TelemetryPublisher
from host_modules.healthz_catalog import HealthzCatalog
from tests.dldd_fakes import FakeStateDB


def _runtime_values():
    return {
        TelemetryPublisher.STATUS_KEY: {"state": "OK"},
        TelemetryPublisher.RULE_STATUS_KEY: {"rule_count": "1"},
        TelemetryPublisher.RULE_STATUS_PREFIX + "RULE_A": {"health": "OK"},
        TelemetryPublisher.RULE_DETAIL_PREFIX + "RULE_A": {"rule": "RULE_A"},
        "FAULT_INFO|PSU0|DLDD": {
            "producer": "dldd",
            "component_name": "PSU0",
            "component_type": "POWER_SUPPLY",
            "symptom": "DLDD",
            "status": "ACTIVE",
            "last_detection_time": "99",
            "healthz_artifact_id": "original-archive",
            "rule_id": "1000001",
            "rule": "RULE_A",
            "schema_version": "0.0.1",
            "active_rules_checksum": "sha256:test",
        },
        "FAULT_INFO|PSU0|FOREIGN": {"producer": "another-service"},
        "FAULT_INFO|PSU0|LOOKALIKE": {
            "rule_id": "1000001",
            "active_rules_checksum": "sha256:test",
        },
        "UNRELATED|key": {"value": "preserve"},
    }


def test_fault_ownership_and_cleanup_contract(tmp_path):
    assert is_dldd_fault_payload({"producer": "dldd"})
    assert not is_dldd_fault_payload({"producer": "another-service"})

    values = _runtime_values()
    state_db = FakeStateDB(values)
    state_file = tmp_path / "dld_state.json"
    state_file.write_text("{}", encoding="utf-8")
    artifacts = tmp_path / "artifacts-full"
    artifacts.mkdir()
    artifact = artifacts / "dldd-test.tar.gz"
    artifact.write_text("diagnostics", encoding="utf-8")

    result = clear_runtime_state(
        state_db,
        str(state_file),
        artifact_directory=str(artifacts),
    )

    assert not state_file.exists()
    assert artifact.exists()
    assert "FAULT_INFO|PSU0|DLDD" in values
    assert "FAULT_INFO|PSU0|FOREIGN" in values
    assert "FAULT_INFO|PSU0|LOOKALIKE" in values
    assert "UNRELATED|key" in values
    assert not any(key.startswith("DLDD_") for key in values)
    assert result.redis_keys == 4
    assert result.faults == 0
    assert result.artifacts == 0
    assert result.local_state_removed

    values = _runtime_values()
    state_db = FakeStateDB(values)
    state_file = tmp_path / "missing-state.json"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "dldd-test.tar.gz").write_text("data", encoding="utf-8")
    (artifacts / "dldd-test.tar.gz.json").write_text(
        "{}", encoding="utf-8"
    )
    (artifacts / ".dldd-test-staged.tar.gz").write_text(
        "staged", encoding="utf-8"
    )
    (artifacts / "foreign.tar.gz").write_text("keep", encoding="utf-8")

    result = clear_runtime_state(
        state_db,
        str(state_file),
        include_faults=True,
        include_artifacts=True,
        artifact_directory=str(artifacts),
    )

    assert "FAULT_INFO|PSU0|DLDD" not in values
    clear = state_db.streams[TelemetryPublisher.FAULT_TRANSITIONS_STREAM][0][1]
    assert {name: clear[name] for name in (
        "producer", "source_key", "component", "symptom", "active"
    )} == {
        "producer": "dldd",
        "source_key": "FAULT_INFO|PSU0|DLDD",
        "component": "PSU0",
        "symptom": "DLDD",
        "active": "0",
    }
    assert clear["transition_id"]
    assert clear["last_unhealthy_at"] == "99"
    assert "artifact_id" not in clear
    assert "replay" not in clear
    assert "FAULT_INFO|PSU0|FOREIGN" in values
    assert "FAULT_INFO|PSU0|LOOKALIKE" in values
    assert "UNRELATED|key" in values
    assert (artifacts / "foreign.tar.gz").exists()
    assert not (artifacts / "dldd-test.tar.gz").exists()
    assert not (artifacts / "dldd-test.tar.gz.json").exists()
    assert not (artifacts / ".dldd-test-staged.tar.gz").exists()
    assert result.redis_keys == 5
    assert result.faults == 1
    assert result.artifacts == 3
    assert not result.local_state_removed


def test_clear_all_updates_healthz_and_preserves_rows_on_write_failure(
    tmp_path, monkeypatch
):
    values = _runtime_values()
    state_db = FakeStateDB(values)
    key = "FAULT_INFO|PSU0|DLDD"
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    try:
        active = {
            "producer": "dldd",
            "source_key": key,
            "transition_id": "initial",
            "component": "PSU0",
            "component_type": "POWER_SUPPLY",
            "symptom": "DLDD",
            "active": "1",
            "observed_at": "100",
        }
        assert catalog.apply_transition("1-0", active)
        assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"

        state_db.fail_writes_with(RuntimeError("stream unavailable"))
        with pytest.raises(RuntimeError, match="stream unavailable"):
            clear_runtime_state(state_db, str(tmp_path / "state.json"), include_faults=True)
        assert key in values
        assert state_db.delete_calls == 0
        assert TelemetryPublisher.FAULT_TRANSITIONS_STREAM not in state_db.streams

        state_db.clear_failures()
        clear_with_transitions = state_db.clear_with_transitions

        def fail_clear(*_args):
            raise RuntimeError("transaction unavailable")

        monkeypatch.setattr(state_db, "clear_with_transitions", fail_clear)
        with pytest.raises(RuntimeError, match="transaction unavailable"):
            clear_runtime_state(state_db, str(tmp_path / "state.json"), include_faults=True)
        assert key in values
        assert TelemetryPublisher.FAULT_TRANSITIONS_STREAM not in state_db.streams

        monkeypatch.setattr(state_db, "clear_with_transitions", clear_with_transitions)
        clear_runtime_state(state_db, str(tmp_path / "state.json"), include_faults=True)
        clears = [entry for _, entry in state_db.streams[
            TelemetryPublisher.FAULT_TRANSITIONS_STREAM
        ]]
        assert len(clears) == 1
        assert catalog.apply_transition("2-0", clears[0])
        assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
        assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
        assert key not in values
    finally:
        catalog.close()


def test_clear_all_commits_multiple_fault_transitions_and_deletions(tmp_path):
    values = _runtime_values()
    second = "FAULT_INFO|PSU1|DLDD"
    values[second] = dict(values["FAULT_INFO|PSU0|DLDD"], component_name="PSU1")
    state_db = FakeStateDB(values)

    result = clear_runtime_state(state_db, str(tmp_path / "state.json"), include_faults=True)

    clears = [entry for _, entry in state_db.streams[
        TelemetryPublisher.FAULT_TRANSITIONS_STREAM
    ]]
    assert {entry["source_key"] for entry in clears} == {
        "FAULT_INFO|PSU0|DLDD", second,
    }
    assert all(entry["active"] == "0" for entry in clears)
    assert result.faults == 2
    assert "FAULT_INFO|PSU0|DLDD" not in values
    assert second not in values
    assert "FAULT_INFO|PSU0|FOREIGN" in values


def test_clear_all_deletes_retained_inactive_without_replay(tmp_path):
    key = "FAULT_INFO|PSU0|DLDD"
    values = _runtime_values()
    values[key].update({
        "status": "INACTIVE",
        "healthz_artifact_id": "oldarchive",
    })
    state_db = FakeStateDB(values)
    result = clear_runtime_state(
        state_db, str(tmp_path / "state.json"), include_faults=True
    )

    assert key not in values
    assert "FAULT_INFO|PSU0|FOREIGN" in values
    assert "FAULT_INFO|PSU0|LOOKALIKE" in values
    assert TelemetryPublisher.FAULT_TRANSITIONS_STREAM not in state_db.streams
    assert result.faults == 1
    assert result.redis_keys == 5


def test_clear_all_emits_only_active_clear_with_mixed_faults(tmp_path):
    active_key = "FAULT_INFO|PSU0|DLDD"
    inactive_key = "FAULT_INFO|PSU1|DLDD"
    values = _runtime_values()
    values[inactive_key] = dict(
        values[active_key],
        component_name="PSU1",
        status="INACTIVE",
        healthz_artifact_id="recovery-archive",
    )
    state_db = FakeStateDB(values)
    result = clear_runtime_state(
        state_db, str(tmp_path / "state.json"), include_faults=True
    )

    transitions = [entry for _, entry in state_db.streams[
        TelemetryPublisher.FAULT_TRANSITIONS_STREAM
    ]]
    assert len(transitions) == 1
    assert transitions[0]["source_key"] == active_key
    assert transitions[0]["active"] == "0"
    assert "artifact_id" not in transitions[0]
    assert "replay" not in transitions[0]
    assert active_key not in values
    assert inactive_key not in values
    assert "FAULT_INFO|PSU0|FOREIGN" in values
    assert result.faults == 2
