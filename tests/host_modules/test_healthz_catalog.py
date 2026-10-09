"""Generic Healthz catalog lifecycle and durability contract."""

import os
import sqlite3
import stat
from unittest.mock import patch

import pytest

from host_modules.healthz_catalog import HealthzCatalog


def transition(component="PSU0", symptom="OVER_TEMP", active=True,
               artifact_id=None, observed_at=100, transition_id=None, **extra):
    row = {
        "producer": "dldd",
        "source_key": f"FAULT_INFO|{component}|{symptom}",
        "transition_id": transition_id or f"{component}/{symptom}/{active}/{observed_at}",
        "component": component,
        "component_type": "PSU",
        "symptom": symptom,
        "active": "1" if active else "0",
        "observed_at": str(observed_at),
        **extra,
    }
    if artifact_id:
        row["artifact_id"] = artifact_id
    return row


@pytest.mark.parametrize(
    "field", ["observed_at", "retain_until", "last_unhealthy_at"]
)
def test_oversized_transition_time_does_not_poison_catalog(tmp_path, field):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    row = transition(active=False)
    row[field] = "10000000000"
    with pytest.raises(ValueError, match="timestamp"):
        catalog.apply_transition("100-0", row)
    assert catalog.get_checkpoint() is None
    assert catalog.list_events() == []
    assert catalog.apply_transition("101-0", transition(active=False))
    assert catalog.get_checkpoint() == "101-0"
    catalog.close()


def test_events_overlap_ack_and_restart(tmp_path):
    path = tmp_path / "healthz" / "catalog.sqlite3"
    catalog = HealthzCatalog(path)
    first_transition = transition(artifact_id="dldd-one.tar.gz")
    assert catalog.apply_transition("100-0", first_transition)
    first = catalog.get_latest("PSU0")
    assert first["id"] == first["artifact_id"] == "dldd-one.tar.gz"
    assert first["status"] == "UNHEALTHY"
    assert catalog.get_aggregate("PSU0") == {
        "component": "PSU0", "status": "UNHEALTHY",
        "last_unhealthy": 100000000000, "unhealthy_count": 1,
    }
    assert not catalog.apply_transition("100-0", first_transition)

    # Two active sources yield two events but one unhealthy period.
    catalog.apply_transition(
        "101-0", transition(symptom="FAN_STOPPED", observed_at=101)
    )
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 101000000000
    catalog.apply_transition(
        "102-0", transition(active=False, observed_at=102)
    )
    assert len(catalog.list_events("PSU0")) == 2
    assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    last_transition = transition(symptom="FAN_STOPPED", active=False,
                                 observed_at=103)
    catalog.apply_transition("103-0", last_transition)
    recovery = catalog.get_latest("PSU0")
    assert recovery["status"] == "HEALTHY"
    assert recovery["id"].startswith("hz-")
    assert recovery["id"] != first["id"]
    assert recovery["artifact_id"] is None
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_checkpoint() == "103-0"

    assert catalog.acknowledge("PSU0", first["id"])["acknowledged"]
    assert catalog.acknowledge("PSU0", first["id"])["acknowledged"]
    assert catalog.acknowledge("PSU1", first["id"]) is None
    assert len(catalog.list_events("PSU0")) == 2
    assert len(catalog.list_events("PSU0", include_acknowledged=True)) == 3
    catalog.close()

    reopened = HealthzCatalog(path)
    assert reopened.get_checkpoint() == "103-0"
    assert reopened.get_latest("PSU0") == recovery
    assert reopened.acknowledge("PSU0", first["id"])["artifact_id"] == first["id"]
    assert not reopened.apply_transition(
        "104-0", {**last_transition, "replay": "1"}
    )
    assert reopened.get_checkpoint() == "104-0"
    assert len(reopened.list_events("PSU0", include_acknowledged=True)) == 3
    reopened.close()


def test_inactive_only_recovery_uses_new_archive_id(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    row = transition(active=False, artifact_id="dldd-recovered.tar.gz",
                     last_unhealthy_at="98")
    assert catalog.apply_transition("100-0", row)
    event = catalog.get_latest("PSU0")
    assert event["id"] == event["artifact_id"] == "dldd-recovered.tar.gz"
    assert event["status"] == "HEALTHY"
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 0
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 98000000000
    assert not catalog.apply_transition("101-0", {**row, "replay": "1"})
    assert len(catalog.list_events()) == 1
    catalog.close()


def test_duplicate_transition_id_does_not_change_aggregate(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    row = transition(active=False, observed_at=100)
    assert catalog.apply_transition("100-0", row)
    aggregate = catalog.get_aggregate("PSU0")
    events = catalog.list_events("PSU0")
    assert not catalog.apply_transition(
        "101-0", {**row, "last_unhealthy_at": "98", "replay": "1"}
    )
    assert catalog.get_aggregate("PSU0") == aggregate
    assert catalog.list_aggregates() == [aggregate]  # Projection source is unchanged.
    assert catalog.list_events("PSU0") == events
    assert catalog.get_checkpoint() == "101-0"
    catalog.close()


def test_inactive_only_archive_is_visible_during_overlapping_fault(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition("100-0", transition(artifact_id="dldd-active.tar.gz"))
    catalog.apply_transition(
        "101-0", transition(symptom="FAN_STOPPED", active=False,
                            observed_at=101, artifact_id="dldd-recovered.tar.gz")
    )
    event = catalog.get_latest("PSU0")
    assert event["id"] == event["artifact_id"] == "dldd-recovered.tar.gz"
    assert event["status"] == "UNHEALTHY"
    assert catalog.get_aggregate("PSU0") == {
        "component": "PSU0", "status": "UNHEALTHY",
        "last_unhealthy": 100000000000, "unhealthy_count": 1,
    }
    assert len(catalog.list_events("PSU0")) == 2
    catalog.close()


def test_overlapping_recovery_does_not_readvertise_old_archive(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition("100-0", transition(artifact_id="dldd-active.tar.gz"))
    catalog.apply_transition(
        "101-0", transition(symptom="FAN_STOPPED", observed_at=101,
                            artifact_id="dldd-fan.tar.gz")
    )
    catalog.apply_transition(
        "102-0", transition(symptom="FAN_STOPPED", active=False,
                            observed_at=102, artifact_id="dldd-fan.tar.gz")
    )
    assert catalog.get_latest("PSU0")["id"] == "dldd-fan.tar.gz"
    assert len(catalog.list_events("PSU0")) == 2
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    catalog.close()


def test_replay_after_event_pruning_does_not_duplicate_archive(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3", max_events=1)
    row = transition(artifact_id="dldd-original.tar.gz")
    catalog.apply_transition("100-0", row)
    catalog.apply_transition(
        "101-0", transition(component="PSU1", observed_at=101)
    )
    assert catalog.get_latest("PSU0") is None
    assert not catalog.apply_transition("102-0", {**row, "replay": "1"})
    assert catalog.get_latest("PSU0") is None
    assert catalog.get_checkpoint() == "102-0"
    catalog.apply_transition("103-0", transition(active=False, observed_at=103))
    recovered = catalog.get_latest("PSU0")
    assert recovered["id"] != "dldd-original.tar.gz"
    assert recovered["artifact_id"] is None
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    catalog.close()


def test_inactive_source_id_survives_retention_deadline_grace(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    now = 1_000_000_000
    row = transition(active=False, observed_at=now,
                     retain_until=str(now + 10))
    with patch("host_modules.healthz_catalog.time.time", return_value=now):
        catalog.apply_transition("100-0", row)
    with patch("host_modules.healthz_catalog.time.time", return_value=now + 10):
        catalog.apply_transition(
            "101-0", transition(component="PSU1", observed_at=now + 10)
        )
        assert not catalog.apply_transition("102-0", {**row, "replay": "1"})
    assert catalog._db.execute(
        "SELECT 1 FROM sources WHERE source_key=?", (row["source_key"],)
    ).fetchone()
    with patch("host_modules.healthz_catalog.time.time", return_value=now + 70):
        catalog.apply_transition(
            "103-0", transition(component="PSU2", observed_at=now + 70)
        )
    assert catalog._db.execute(
        "SELECT 1 FROM sources WHERE source_key=?", (row["source_key"],)
    ).fetchone() is None
    catalog.close()


def test_pruning_prefers_acknowledged_but_keeps_latest_per_component(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3", max_events=2)
    catalog.apply_transition("100-0", transition())
    old = catalog.get_latest("PSU0")
    catalog.apply_transition("101-0", transition(active=False, observed_at=101))
    recovery = catalog.get_latest("PSU0")
    catalog.acknowledge("PSU0", recovery["id"])
    catalog.apply_transition("102-0", transition(component="PSU1", observed_at=102))
    assert catalog.get_latest("PSU0")["id"] == recovery["id"]
    assert old["id"] not in {
        event["id"] for event in catalog.list_events(include_acknowledged=True)
    }
    catalog.apply_transition("103-0", transition(component="PSU2", observed_at=103))
    assert catalog.get_latest("PSU0") is None
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    catalog.close()


def test_same_artifact_for_different_transition_is_not_advertised_twice(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition(
        "100-0", transition(artifact_id="dldd-one.tar.gz")
    )
    catalog.apply_transition(
        "101-0", transition(component="PSU1", observed_at=101,
                            artifact_id="dldd-one.tar.gz")
    )
    assert catalog.get_checkpoint() == "101-0"
    assert catalog.get_latest("PSU1")["id"].startswith("hz-")
    assert catalog.get_latest("PSU1")["artifact_id"] is None
    catalog.close()


def test_observation_updates_last_unhealthy_without_event(tmp_path):
    catalog = HealthzCatalog(tmp_path / "catalog.sqlite3")
    catalog.apply_transition("100-0", transition())
    before = catalog.get_latest("PSU0")
    observation = {
        "kind": "observation", "producer": "dldd",
        "source_key": "FAULT_INFO|PSU0|OVER_TEMP",
        "component": "PSU0", "observed_at": "110",
    }
    assert catalog.apply_transition("101-0", observation)
    assert catalog.get_checkpoint() == "101-0"
    assert catalog.get_latest("PSU0") == before
    assert catalog.get_aggregate("PSU0") == {
        "component": "PSU0", "status": "UNHEALTHY",
        "last_unhealthy": 110000000000, "unhealthy_count": 1,
    }
    catalog.apply_transition(
        "102-0", transition(symptom="FAN_STOPPED", observed_at=115)
    )
    catalog.apply_transition("103-0", transition(active=False, observed_at=120))
    assert not catalog.apply_transition(
        "104-0", {**observation, "observed_at": "130"}
    )
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 115000000000
    assert catalog.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    catalog.apply_transition(
        "105-0", transition(symptom="FAN_STOPPED", active=False,
                            observed_at=140)
    )
    assert catalog.get_aggregate("PSU0")["status"] == "HEALTHY"
    assert catalog.get_aggregate("PSU0")["last_unhealthy"] == 115000000000
    catalog.close()


def test_migrates_legacy_membership_without_losing_acknowledgement(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
            stream_id TEXT NOT NULL, component TEXT NOT NULL, component_type TEXT,
            symptom TEXT NOT NULL, status TEXT NOT NULL, observed_at INTEGER NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0, artifact_id TEXT
        );
        CREATE TABLE faults (
            fault_key TEXT PRIMARY KEY, producer TEXT NOT NULL,
            component TEXT NOT NULL, symptom TEXT NOT NULL,
            occurrence INTEGER NOT NULL, active INTEGER NOT NULL,
            stream_seen INTEGER NOT NULL, artifact_id TEXT
        );
        CREATE TABLE aggregates (
            component TEXT PRIMARY KEY, status TEXT NOT NULL,
            last_unhealthy INTEGER, unhealthy_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO metadata(name,value) VALUES('checkpoint','150-0');
        INSERT INTO metadata(name,value) VALUES('gap_reason','previous stream gap');
        INSERT INTO metadata(name,value) VALUES('gap_recorded_at','99');
        INSERT INTO metadata(name,value) VALUES('gap_first_available_id','140-0');
        INSERT INTO events(event_id,stream_id,component,component_type,symptom,
                           status,observed_at,acknowledged,artifact_id)
            VALUES('dldd-one.tar.gz','100-0','PSU0','PSU','OVER_TEMP',
                   'UNHEALTHY',100,1,'dldd-one.tar.gz');
        INSERT INTO faults(fault_key,producer,component,symptom,occurrence,
                           active,stream_seen,artifact_id)
            VALUES('FAULT_INFO|PSU0|OVER_TEMP','dldd','PSU0','OVER_TEMP',1,
                   1,1,'dldd-one.tar.gz');
        INSERT INTO aggregates(component,status,last_unhealthy,unhealthy_count)
            VALUES('PSU0','UNHEALTHY',100000000000,1);
        """
    )
    db.close()
    catalog = HealthzCatalog(path)
    assert catalog.get_checkpoint() is None  # New stream has its own checkpoint.
    gap = catalog.get_gap()
    assert gap["reason"].startswith("legacy DLDD transition stream tail not replayed")
    assert "previous stream gap" in gap["reason"]
    assert gap["first_available_id"] is None
    catalog.close()
    catalog = HealthzCatalog(path)
    assert catalog.get_gap() == gap
    assert catalog.get_checkpoint() is None
    assert catalog.apply_transition(
        "200-0", transition(artifact_id="dldd-one.tar.gz", replay="1")
    )
    # A migrated source seeds the new producer transition ID without making a
    # second event.  Its retained event and acknowledgement survive intact.
    event = catalog.get_latest("PSU0")
    assert event["id"] == "dldd-one.tar.gz"
    assert event["acknowledged"]
    assert len(catalog.list_events(include_acknowledged=True)) == 1
    assert catalog.get_aggregate("PSU0")["unhealthy_count"] == 1
    assert catalog.get_checkpoint() == "200-0"
    catalog.close()
    reopened = HealthzCatalog(path)
    assert reopened.get_gap() == gap
    assert reopened.get_checkpoint() == "200-0"
    assert reopened.get_latest("PSU0")["acknowledged"]
    reopened.close()


def test_catalog_paths_gap_and_redis_identity(tmp_path):
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
    catalog.apply_transition("100-0", transition())
    catalog.mark_gap(
        "Redis stream trimmed before checkpoint",
        first_available_id="201-0", resume_after_id="200-0",
    )
    catalog.set_redis_run_id("redis-process-a")
    catalog.close()

    reopened = HealthzCatalog(path)
    assert reopened.get_checkpoint() == "200-0"
    assert reopened.get_gap()["reason"] == "Redis stream trimmed before checkpoint"
    assert reopened.get_gap()["first_available_id"] == "201-0"
    assert reopened.get_aggregate("PSU0")["status"] == "UNHEALTHY"
    assert reopened.get_redis_run_id() == "redis-process-a"
    reopened.mark_gap("Redis run_id changed")
    assert reopened.get_gap()["first_available_id"] is None
    with pytest.raises(ValueError, match="too long"):
        reopened.set_redis_run_id("x" * 129)
    reopened.close()

    reopened = HealthzCatalog(path)
    assert reopened.get_gap()["reason"] == "Redis run_id changed"
    assert reopened.get_gap()["first_available_id"] is None
    reopened.close()

    link = tmp_path / "catalog-link.sqlite3"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        HealthzCatalog(link)
    directory_link = tmp_path / "healthz-link"
    directory_link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError):
        HealthzCatalog(directory_link / "catalog.sqlite3")
