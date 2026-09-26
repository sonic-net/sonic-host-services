"""Healthz catalog lifecycle and durability contract."""

import os
import stat

from host_modules.healthz_catalog import HealthzCatalog


def transition(component="PSU0", symptom="OVER_TEMP", status="ACTIVE",
               occurrence=1, artifact_id=None, observed_at=100):
    row = {
        "producer": "dldd",
        "fault_key": f"FAULT_INFO|{component}|{symptom}",
        "component": component,
        "component_type": "PSU",
        "symptom": symptom,
        "status": status,
        "occurrence": occurrence,
        "observed_at": observed_at,
    }
    if artifact_id:
        row["artifact_id"] = artifact_id
    return row


def test_stream_events_aggregate_ack_and_restart(tmp_path):
    path = tmp_path / "healthz" / "catalog.sqlite3"
    catalog = HealthzCatalog(path)
    assert catalog.apply_transition(
        "100-0", transition(artifact_id="dldd-one.tar.gz")
    )
    first = catalog.get_latest("PSU0")
    assert first["id"] == "dldd-one.tar.gz"
    assert first["artifact_id"] == first["id"]
    assert first["status"] == "UNHEALTHY"
    assert catalog.get_aggregate("PSU0") == {
        "component": "PSU0", "status": "UNHEALTHY",
        "last_unhealthy": 100000000000, "unhealthy_count": 1,
    }
    assert not catalog.apply_transition(
        "100-0", transition(artifact_id="dldd-one.tar.gz")
    )

    # A second fault is a second event, but not a second component transition.
    assert catalog.apply_transition(
        "101-0", transition(symptom="FAN_STOPPED", observed_at=101)
    )
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 101000000000
    assert catalog.apply_transition(
        "102-0", transition(status="INACTIVE", artifact_id="dldd-one.tar.gz",
                            observed_at=102)
    )
    assert len(catalog.list_events("PSU0")) == 2
    assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    assert catalog.apply_transition(
        "103-0", transition(symptom="FAN_STOPPED", status="INACTIVE",
                            observed_at=103)
    )
    recovered = catalog.get_latest("PSU0")
    assert recovered["status"] == "HEALTHY"
    assert recovered["id"].startswith("hz-")
    assert recovered["id"] != first["id"]
    assert recovered["artifact_id"] is None
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 101000000000
    assert catalog.get_checkpoint() == "103-0"
    assert catalog.acknowledge("PSU0", first["id"])["acknowledged"]
    assert catalog.acknowledge("PSU0", first["id"])["acknowledged"]
    assert catalog.acknowledge("PSU1", first["id"]) is None
    assert len(catalog.list_events("PSU0")) == 2
    assert len(catalog.list_events("PSU0", include_acknowledged=True)) == 3
    catalog.close()

    reopened = HealthzCatalog(path)
    assert reopened.get_checkpoint() == "103-0"
    assert reopened.get_latest("PSU0") == recovered
    assert reopened.acknowledge("PSU0", first["id"])["artifact_id"] == first["id"]
    assert not reopened.apply_transition(
        "102-0", transition(status="INACTIVE", artifact_id="dldd-one.tar.gz",
                            observed_at=102)
    )
    reopened.close()


def test_inactive_only_recovery_and_new_archive_id(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    row = transition(status="INACTIVE", artifact_id="dldd-recovered.tar.gz")
    assert catalog.apply_transition("100-0", row)
    event = catalog.get_latest("PSU0")
    assert event["id"] == event["artifact_id"] == "dldd-recovered.tar.gz"
    assert event["status"] == "HEALTHY"
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 0
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] is None
    assert not catalog.apply_transition("100-0", row)
    assert len(catalog.list_events()) == 1
    catalog.close()


def test_recovery_carried_artifact_is_not_advertised_twice_even_after_prune(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3", max_events=1)
    assert catalog.apply_transition(
        "100-0", transition(artifact_id="dldd-original.tar.gz")
    )
    # Force the original event out of retention; the fault episode still owns
    # the claim and the recovery cannot treat its copied ID as a new archive.
    assert catalog.apply_transition(
        "101-0", transition(component="PSU1", observed_at=101)
    )
    assert catalog.apply_transition(
        "102-0", transition(status="INACTIVE", artifact_id="dldd-original.tar.gz",
                            observed_at=102)
    )
    recovery = catalog.get_latest("PSU0")
    assert recovery["id"] != "dldd-original.tar.gz"
    assert recovery["artifact_id"] is None
    assert len(catalog.list_events()) == 1
    catalog.close()


def test_prune_acknowledged_first(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3", max_events=2)
    catalog.apply_transition("100-0", transition(component="PSU0"))
    old = catalog.get_latest("PSU0")
    catalog.apply_transition("101-0", transition(component="PSU1"))
    catalog.acknowledge("PSU1", catalog.get_latest("PSU1")["id"])
    catalog.apply_transition("102-0", transition(component="PSU2"))
    assert catalog.get_latest("PSU0") == old
    assert catalog.get_latest("PSU1") is None
    assert len(catalog.list_events(include_acknowledged=True)) == 2
    catalog.close()


def test_snapshot_records_current_state_without_inventing_missing_history(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "ACTIVE", "occurrences": "1", "last_detection_time": "200",
    }])
    observation = catalog.get_latest("PSU0")
    assert observation["status"] == "UNHEALTHY"
    assert observation["source"] == "snapshot"
    assert observation["observed_at"] == 200
    assert len(catalog.list_events()) == 1
    assert catalog.get_checkpoint() is None
    assert catalog.get_aggregate("PSU0") == {
        "component": "PSU0", "status": "UNHEALTHY",
        "last_unhealthy": 200000000000, "unhealthy_count": 1,
    }
    catalog.reconcile_snapshot([])
    assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "ACTIVE", "occurrences": "1", "last_detection_time": "210",
    }])
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 210000000000
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert len(catalog.list_events()) == 1
    # The snapshot's last_detection_time advanced to 210, while the delayed
    # transition was observed at 200.  The matching fault episode still has
    # one event, and replay advances only the stream checkpoint.
    catalog.apply_transition("100-0", transition(
        observed_at=200, artifact_id="dldd-late.tar.gz"
    ))
    assert len(catalog.list_events()) == 1
    assert catalog.get_latest("PSU0")["id"] == observation["id"]
    assert catalog.get_latest("PSU0")["artifact_id"] == "dldd-late.tar.gz"
    assert catalog.get_checkpoint() == "100-0"
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 210000000000
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "INACTIVE", "occurrences": "1", "last_detection_time": "210",
        "inactive_since": "220",
        "healthz_artifact": '{"artifact_id":"dldd-late.tar.gz"}',
    }])
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    assert len(catalog.list_events()) == 2
    recovery = catalog.get_latest("PSU0")
    assert recovery["source"] == "snapshot"
    assert recovery["observed_at"] == 220
    assert recovery["artifact_id"] is None
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 210000000000
    catalog.reconcile_snapshot([])
    assert len(catalog.list_events()) == 2
    catalog.close()


def test_active_snapshot_does_not_suppress_later_stream_recovery(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "ACTIVE", "occurrences": "1", "last_detection_time": "100",
    }])
    assert catalog.apply_transition(
        "200-0", transition(status="INACTIVE", observed_at=200)
    )
    events = catalog.list_events("PSU0", include_acknowledged=True)
    assert [event["status"] for event in events] == ["HEALTHY", "UNHEALTHY"]
    assert events[0]["source"] == "stream"
    assert catalog.get_latest("PSU0")["status"] == "HEALTHY"
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_checkpoint() == "200-0"
    catalog.close()


def test_snapshot_gap_get_matches_observed_state_and_carries_no_old_archive(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition("100-0", transition(artifact_id="dldd-old.tar.gz"))
    catalog.mark_gap("unconsumed stream was lost", first_available_id="300-0")
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "INACTIVE", "occurrences": "1", "inactive_since": "250",
        "healthz_artifact": '{"artifact_id":"dldd-old.tar.gz"}',
    }])
    latest = catalog.get_latest("PSU0")
    assert latest["source"] == "snapshot"
    assert latest["status"] == "HEALTHY"
    assert latest["artifact_id"] is None
    assert latest["id"] != "dldd-old.tar.gz"
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    assert catalog.get_checkpoint() == "100-0"
    catalog.close()


def test_snapshot_inactive_only_new_archive_uses_artifact_id(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "INACTIVE", "occurrences": "1", "inactive_since": "250",
        "healthz_artifact": '{"artifact_id":"dldd-new.tar.gz"}',
    }])
    event = catalog.get_latest("PSU0")
    assert event["source"] == "snapshot"
    assert event["status"] == "HEALTHY"
    assert event["id"] == event["artifact_id"] == "dldd-new.tar.gz"
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 0
    catalog.reconcile_snapshot([{
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "INACTIVE", "occurrences": "1", "inactive_since": "250",
        "healthz_artifact": '{"artifact_id":"dldd-new.tar.gz"}',
    }])
    assert len(catalog.list_events()) == 1
    catalog.close()


def test_late_archive_links_to_existing_event_without_transition(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition("100-0", transition())
    original = catalog.get_latest("PSU0")
    row = {
        "producer": "dldd", "component_name": "PSU0", "symptom": "OVER_TEMP",
        "status": "ACTIVE", "occurrences": "1", "last_detection_time": "110",
        "healthz_artifact": '{"artifact_id":"dldd-late.tar.gz"}',
    }
    catalog.reconcile_snapshot([row])
    latest = catalog.get_latest("PSU0")
    assert latest["id"] == original["id"]
    assert latest["artifact_id"] == "dldd-late.tar.gz"
    assert len(catalog.list_events()) == 1
    catalog.reconcile_snapshot([row])
    assert len(catalog.list_events()) == 1
    assert catalog.get_latest("PSU0")["artifact_id"] == "dldd-late.tar.gz"
    catalog.apply_transition(
        "120-0", transition(status="INACTIVE", artifact_id="dldd-late.tar.gz",
                            observed_at=120)
    )
    recovery = catalog.get_latest("PSU0")
    assert recovery["id"] != "dldd-late.tar.gz"
    assert recovery["artifact_id"] is None
    catalog.close()


def test_catalog_restricts_existing_paths_and_rejects_symlinks(tmp_path):
    directory = tmp_path / "healthz"
    directory.mkdir(mode=0o777)
    path = directory / "catalog.sqlite3"
    path.write_bytes(b"")
    os.chmod(path, 0o666)
    catalog = HealthzCatalog(path)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    page_size = catalog._db.execute("PRAGMA page_size").fetchone()[0]
    max_pages = catalog._db.execute("PRAGMA max_page_count").fetchone()[0]
    assert page_size * max_pages <= 32 * 1024 * 1024
    catalog.close()

    link = tmp_path / "catalog-link.sqlite3"
    link.symlink_to(path)
    try:
        HealthzCatalog(link)
    except ValueError:
        pass
    else:
        raise AssertionError("symlink database path accepted")

    directory_link = tmp_path / "healthz-link"
    directory_link.symlink_to(directory, target_is_directory=True)
    try:
        HealthzCatalog(directory_link / "catalog.sqlite3")
    except ValueError:
        pass
    else:
        raise AssertionError("symlink directory accepted")


def test_gap_marker_and_checkpoint_survive_restart(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    catalog = HealthzCatalog(path)
    catalog.apply_transition("100-0", transition())
    catalog.mark_gap(
        "Redis stream trimmed before checkpoint",
        first_available_id="201-0", resume_after_id="200-0",
    )
    assert catalog.get_checkpoint() == "200-0"
    assert catalog.get_gap()["reason"] == "Redis stream trimmed before checkpoint"
    assert catalog.get_gap()["first_available_id"] == "201-0"
    assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    catalog.close()
    reopened = HealthzCatalog(path)
    assert reopened.get_gap()["reason"] == "Redis stream trimmed before checkpoint"
    assert reopened.get_checkpoint() == "200-0"
    assert len(reopened.list_events()) == 1
    reopened.close()


def test_redis_run_id_survives_restart_and_is_bounded(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    catalog = HealthzCatalog(path)
    assert catalog.get_redis_run_id() is None
    catalog.set_redis_run_id("redis-process-a")
    catalog.close()

    reopened = HealthzCatalog(path)
    assert reopened.get_redis_run_id() == "redis-process-a"
    reopened.set_redis_run_id("redis-process-b")
    try:
        reopened.set_redis_run_id("x" * 129)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized Redis run ID accepted")
    assert reopened.get_redis_run_id() == "redis-process-b"
    reopened.close()
