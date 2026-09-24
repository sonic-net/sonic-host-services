from __future__ import absolute_import

from dldd.ownership import is_dldd_fault_payload
from dldd.reset import clear_runtime_state
from dldd.telemetry import TelemetryPublisher
from tests.dldd_fakes import FakeStateDB


def _runtime_values():
    return {
        TelemetryPublisher.STATUS_KEY: {"state": "OK"},
        TelemetryPublisher.RULE_STATUS_KEY: {"rule_count": "1"},
        TelemetryPublisher.RULE_STATUS_PREFIX + "RULE_A": {"health": "OK"},
        TelemetryPublisher.RULE_DETAIL_PREFIX + "RULE_A": {"rule": "RULE_A"},
        "FAULT_INFO|PSU0|DLDD": {
            "producer": "dldd",
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
