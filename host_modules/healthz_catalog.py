"""Durable, bounded Healthz event and component-state catalog.

The stream consumer owns transition ordering.  This catalog commits each
transition's event, fault membership, component aggregate, and checkpoint in
one SQLite transaction.  Snapshot reconciliation can repair observed state,
but deliberately does not manufacture events for transitions it did not see.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from urllib.parse import quote

DEFAULT_CATALOG_PATH = "/var/lib/sonic/healthz/catalog.sqlite3"
DEFAULT_MAX_EVENTS = 4096
MAX_CATALOG_BYTES = 32 * 1024 * 1024


def _text(value, name):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(f"{name} must be nonempty text of at most 1024 characters")
    return value


def _stream_parts(stream_id):
    stream_id = _text(stream_id, "stream_id")
    parts = stream_id.split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("invalid Redis stream ID")
    return int(parts[0]), int(parts[1])


def _seconds(value):
    if isinstance(value, bool):
        raise TypeError("invalid observation timestamp")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid observation timestamp")
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid observation timestamp")
    return math.floor(number)


def _nanoseconds(value):
    return _seconds(value) * 1000000000 if value not in (None, "") else None


def _artifact_id(value):
    if value in (None, ""):
        return None
    return _text(value, "artifact_id")


def _fault_key(row, producer, component, symptom):
    value = row.get("fault_key")
    if value:
        return _text(value, "fault_key")
    return "FAULT_INFO|{}|{}".format(
        quote(component, safe=""), quote(symptom, safe="")
    )


def _secure_existing(path, directory=False):
    """Restrict an existing catalog path without following a final symlink."""
    info = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError(f"Healthz catalog path has an unexpected file type: {path}")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"Healthz catalog path has an unexpected owner: {path}")
    mode = 0o700 if directory else 0o600
    if stat.S_IMODE(info.st_mode) != mode:
        os.chmod(path, mode, follow_symlinks=False)


def _archive_from_snapshot(row):
    artifact = row.get("healthz_artifact")
    if isinstance(artifact, str):
        try:
            artifact = json.loads(artifact)
        except ValueError:
            artifact = None
    if isinstance(artifact, Mapping):
        return _artifact_id(artifact.get("artifact_id"))
    return _artifact_id(row.get("artifact_id"))


def _event(row):
    if row is None:
        return None
    return {
        "id": row["event_id"],
        "stream_id": row["stream_id"],
        "component": row["component"],
        "component_type": row["component_type"],
        "symptom": row["symptom"],
        "status": row["status"],
        "observed_at": row["observed_at"],
        "acknowledged": bool(row["acknowledged"]),
        "artifact_id": row["artifact_id"],
        "source": row["source"],
    }


def _aggregate(row):
    if row is None:
        return None
    return {
        "component": row["component"],
        "status": row["status"],
        "last_unhealthy": row["last_unhealthy"],
        "unhealthy_count": row["unhealthy_count"],
    }


class HealthzCatalog:
    """Thread-safe SQLite catalog shared by the D-Bus endpoint and worker."""

    def __init__(self, path=DEFAULT_CATALOG_PATH, max_events=DEFAULT_MAX_EVENTS):
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        self.path = os.fspath(path)
        self.max_events = max_events
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(self.path))
        if os.path.islink(directory):
            raise ValueError("Healthz catalog directory must not be a symlink")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _secure_existing(directory, directory=True)
        if not os.path.exists(self.path):
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        _secure_existing(self.path)
        for suffix in ("-wal", "-shm"):
            if os.path.lexists(self.path + suffix):
                _secure_existing(self.path + suffix)
        self._db = sqlite3.connect(
            self.path, timeout=2.0, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            max_pages = MAX_CATALOG_BYTES // page_size
            actual_max = self._db.execute(
                f"PRAGMA max_page_count={MAX_CATALOG_BYTES // page_size}"
            ).fetchone()[0]
            if actual_max > max_pages:
                raise RuntimeError("existing Healthz catalog exceeds the 32 MiB limit")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=2000")
            self._db.execute("PRAGMA journal_size_limit=1048576")
            self._db.execute("PRAGMA wal_autocheckpoint=100")
            for suffix in ("-wal", "-shm"):
                if os.path.lexists(self.path + suffix):
                    _secure_existing(self.path + suffix)
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    stream_id TEXT NOT NULL,
                    component TEXT NOT NULL,
                    component_type TEXT,
                    symptom TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('HEALTHY', 'UNHEALTHY')),
                    observed_at INTEGER NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    artifact_id TEXT,
                    source TEXT NOT NULL DEFAULT 'stream'
                );
                CREATE INDEX IF NOT EXISTS events_component_seq
                    ON events(component, seq DESC);
                CREATE TABLE IF NOT EXISTS faults (
                    fault_key TEXT PRIMARY KEY,
                    producer TEXT NOT NULL,
                    component TEXT NOT NULL,
                    symptom TEXT NOT NULL,
                    occurrence INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    stream_seen INTEGER NOT NULL,
                    artifact_id TEXT,
                    snapshot_event_id TEXT,
                    snapshot_observed_at INTEGER,
                    current_event_id TEXT,
                    seen_artifact_id TEXT,
                    prunable INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS faults_component_active
                    ON faults(component, active);
                CREATE TABLE IF NOT EXISTS aggregates (
                    component TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('HEALTHY', 'UNHEALTHY')),
                    last_unhealthy INTEGER,
                    unhealthy_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS artifact_claims (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # A development image may already contain the initial catalog
            # schema.  These additive columns keep its retained events.
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(events)")}
            if "source" not in columns:
                self._db.execute(
                    "ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'stream'"
                )
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(faults)")}
            if "snapshot_event_id" not in columns:
                self._db.execute("ALTER TABLE faults ADD COLUMN snapshot_event_id TEXT")
            if "snapshot_observed_at" not in columns:
                self._db.execute("ALTER TABLE faults ADD COLUMN snapshot_observed_at INTEGER")
            if "current_event_id" not in columns:
                self._db.execute("ALTER TABLE faults ADD COLUMN current_event_id TEXT")
            if "seen_artifact_id" not in columns:
                self._db.execute("ALTER TABLE faults ADD COLUMN seen_artifact_id TEXT")
            if "prunable" not in columns:
                self._db.execute(
                    "ALTER TABLE faults ADD COLUMN prunable INTEGER NOT NULL DEFAULT 0"
                )

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _meta(self, name):
        row = self._db.execute(
            "SELECT value FROM metadata WHERE name=?", (name,)
        ).fetchone()
        return row["value"] if row else None

    def _set_meta(self, name, value):
        self._db.execute(
            "INSERT INTO metadata(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value),
        )

    def get_checkpoint(self):
        with self._lock:
            return self._meta("checkpoint")

    def get_redis_run_id(self):
        """Return the Redis process identity last seen by the stream worker."""
        with self._lock:
            return self._meta("redis_run_id")

    def set_redis_run_id(self, run_id):
        """Persist a bounded Redis process identity across host restarts."""
        run_id = _text(run_id, "redis_run_id")
        if len(run_id) > 128:
            raise ValueError("redis_run_id is too long")
        with self._transaction():
            self._set_meta("redis_run_id", run_id)

    def set_checkpoint(self, stream_id):
        """Advance past a known stream position without making an event."""
        _stream_parts(stream_id)
        with self._transaction():
            previous = self._meta("checkpoint")
            if previous and _stream_parts(stream_id) < _stream_parts(previous):
                raise ValueError("checkpoint rewind requires mark_gap")
            self._set_meta("checkpoint", stream_id)

    def mark_gap(self, reason, first_available_id=None, resume_after_id=None):
        """Record a known stream loss; optionally reset the replay checkpoint."""
        reason = _text(reason, "gap reason")[:512]
        if first_available_id is not None:
            _stream_parts(first_available_id)
        if resume_after_id is not None:
            _stream_parts(resume_after_id)
        with self._transaction():
            self._set_meta("gap_reason", reason)
            self._set_meta("gap_recorded_at", str(int(time.time())))
            if first_available_id is not None:
                self._set_meta("gap_first_available_id", first_available_id)
            if resume_after_id is not None:
                self._set_meta("checkpoint", resume_after_id)

    def get_gap(self):
        with self._lock:
            reason = self._meta("gap_reason")
            if reason is None:
                return None
            return {
                "reason": reason,
                "recorded_at": int(self._meta("gap_recorded_at")),
                "first_available_id": self._meta("gap_first_available_id"),
            }

    def _active_count(self, component):
        return self._db.execute(
            "SELECT COUNT(*) AS total FROM faults WHERE component=? AND active=1",
            (component,),
        ).fetchone()["total"]

    def _new_event_id(self, artifact_id):
        if artifact_id:
            claimed = self._db.execute(
                "SELECT 1 FROM artifact_claims WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            retained = self._db.execute(
                "SELECT 1 FROM events WHERE event_id=? OR artifact_id=?",
                (artifact_id, artifact_id),
            ).fetchone()
            if not claimed and not retained:
                self._db.execute(
                    "INSERT INTO artifact_claims(artifact_id) VALUES(?)",
                    (artifact_id,),
                )
                return artifact_id, artifact_id
        return f"hz-{uuid.uuid4().hex}", None

    def _insert_event(self, stream_id, component, component_type, symptom,
                      status, observed_at, artifact_id, source="stream"):
        event_id, new_artifact_id = self._new_event_id(artifact_id)
        self._db.execute(
            "INSERT INTO events(event_id,stream_id,component,component_type,"
            "symptom,status,observed_at,artifact_id,source) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, stream_id, component, component_type, symptom,
             status, observed_at, new_artifact_id, source),
        )
        return event_id, new_artifact_id

    def _prune(self):
        count = self._db.execute("SELECT COUNT(*) AS total FROM events").fetchone()["total"]
        excess = count - self.max_events
        if excess > 0:
            self._db.execute(
                "DELETE FROM events WHERE seq IN ("
                "SELECT seq FROM events ORDER BY acknowledged DESC, seq ASC LIMIT ?)" ,
                (excess,),
            )
        faults = self._db.execute(
            "SELECT COUNT(*) AS total FROM faults"
        ).fetchone()["total"]
        excess = faults - (self.max_events * 2)
        if excess > 0:
            # Keep every active fault and inactive fault still represented in
            # retained event history.  If those exceed the cap, the hard SQLite
            # page limit fails closed instead of discarding current state.
            self._db.execute(
                "DELETE FROM faults WHERE rowid IN ("
                "SELECT f.rowid FROM faults f WHERE f.active=0 AND f.prunable=1 "
                "AND NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.component=f.component "
                "AND e.symptom=f.symptom) ORDER BY f.rowid LIMIT ?)",
                (excess,),
            )
        # Artifact IDs remain claimed while their events or fault episodes are
        # retained.  Older unreferenced claims are bounded separately.
        claims = self._db.execute(
            "SELECT COUNT(*) AS total FROM artifact_claims"
        ).fetchone()["total"]
        excess = claims - (self.max_events * 2)
        if excess > 0:
            self._db.execute(
                "DELETE FROM artifact_claims WHERE seq IN ("
                "SELECT a.seq FROM artifact_claims a "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.artifact_id=a.artifact_id) "
                "AND NOT EXISTS (SELECT 1 FROM faults f WHERE f.artifact_id=a.artifact_id) "
                "ORDER BY a.seq LIMIT ?)",
                (excess,),
            )

    def apply_transition(self, stream_id, transition):
        """Apply one ordered DLDD transition, returning False on replay."""
        _stream_parts(stream_id)
        producer = _text(transition["producer"], "producer")
        component = _text(transition["component"], "component")
        symptom = _text(transition["symptom"], "symptom")
        status = _text(transition["status"], "status").upper()
        if status not in ("ACTIVE", "INACTIVE"):
            raise ValueError("invalid fault status")
        fault_key = _fault_key(transition, producer, component, symptom)
        component_type = transition.get("component_type") or None
        if component_type is not None:
            component_type = _text(component_type, "component_type")
        occurrence = int(transition.get("occurrence", 1))
        if occurrence < 1:
            raise ValueError("invalid occurrence")
        observed_at = _seconds(transition["observed_at"])
        artifact_id = _artifact_id(transition.get("artifact_id"))

        with self._transaction():
            checkpoint = self._meta("checkpoint")
            if checkpoint and _stream_parts(stream_id) <= _stream_parts(checkpoint):
                return False
            prior_fault = self._db.execute(
                "SELECT * FROM faults WHERE fault_key=?", (fault_key,)
            ).fetchone()
            prior_aggregate = self._db.execute(
                "SELECT * FROM aggregates WHERE component=?", (component,)
            ).fetchone()
            old_count = self._active_count(component)
            was_active = bool(prior_fault["active"]) if prior_fault else False
            same_occurrence = (
                prior_fault is not None and prior_fault["occurrence"] == occurrence
            )
            stream_seen = bool(prior_fault["stream_seen"]) if prior_fault else False
            last_claimed_artifact = prior_fault["artifact_id"] if prior_fault else None
            snapshot_event_id = (
                prior_fault["snapshot_event_id"] if prior_fault else None
            )
            same_snapshot = bool(
                same_occurrence and snapshot_event_id
                and was_active == (status == "ACTIVE")
            )
            inserted_artifact = None
            inserted_event_id = None

            if status == "ACTIVE":
                new_observation = not (
                    was_active and same_occurrence
                    and (stream_seen or same_snapshot)
                )
                if new_observation:
                    inserted_event_id, inserted_artifact = self._insert_event(
                        stream_id, component, component_type, symptom,
                        "UNHEALTHY", observed_at, artifact_id,
                    )
                if old_count == 0 and (not was_active or not same_occurrence):
                    unhealthy_count = (
                        prior_aggregate["unhealthy_count"] if prior_aggregate else 0
                    ) + 1
                else:
                    unhealthy_count = (
                        prior_aggregate["unhealthy_count"] if prior_aggregate else 0
                    )
                prior_unhealthy = (
                    prior_aggregate["last_unhealthy"] if prior_aggregate else None
                )
                observed_ns = observed_at * 1000000000
                last_unhealthy = max(prior_unhealthy or 0, observed_ns)
                self._db.execute(
                    "INSERT INTO aggregates(component,status,last_unhealthy,unhealthy_count) "
                    "VALUES(?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                    "status=excluded.status,last_unhealthy=excluded.last_unhealthy,"
                    "unhealthy_count=excluded.unhealthy_count",
                    (component, "UNHEALTHY", last_unhealthy, unhealthy_count),
                )
                active = 1
            else:
                active = 0
                remaining = old_count - (1 if was_active else 0)
                explicit_recovery = was_active or not same_occurrence or not stream_seen
                if remaining == 0:
                    if explicit_recovery and not same_snapshot:
                        inserted_event_id, inserted_artifact = self._insert_event(
                            stream_id, component, component_type, symptom,
                            "HEALTHY", observed_at, artifact_id,
                        )
                    self._db.execute(
                        "INSERT INTO aggregates(component,status,last_unhealthy,unhealthy_count) "
                        "VALUES(?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                        "status='HEALTHY'",
                        (component, "HEALTHY",
                         prior_aggregate["last_unhealthy"] if prior_aggregate else None,
                         prior_aggregate["unhealthy_count"] if prior_aggregate else 0),
                    )

            if same_snapshot and artifact_id:
                # A delayed matching stream record can carry an archive that
                # was not yet present in the snapshot.  Keep the snapshot
                # event ID stable while linking the genuinely new archive.
                event = self._db.execute(
                    "SELECT artifact_id FROM events WHERE event_id=? "
                    "AND component=? AND symptom=? AND status=?",
                    (snapshot_event_id, component, symptom,
                     "UNHEALTHY" if status == "ACTIVE" else "HEALTHY"),
                ).fetchone()
                claimed = self._db.execute(
                    "SELECT 1 FROM artifact_claims WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
                if event and event["artifact_id"] is None and not claimed:
                    self._db.execute(
                        "INSERT INTO artifact_claims(artifact_id) VALUES(?)",
                        (artifact_id,),
                    )
                    self._db.execute(
                        "UPDATE events SET artifact_id=? WHERE event_id=?",
                        (artifact_id, snapshot_event_id),
                    )
                    inserted_artifact = artifact_id

            self._db.execute(
                "INSERT INTO faults(fault_key,producer,component,symptom,occurrence,"
                "active,stream_seen,artifact_id,snapshot_event_id,"
                "snapshot_observed_at,current_event_id,seen_artifact_id,prunable) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fault_key) DO UPDATE SET producer=excluded.producer,"
                "component=excluded.component,symptom=excluded.symptom,"
                "occurrence=excluded.occurrence,active=excluded.active,"
                "stream_seen=excluded.stream_seen,artifact_id=excluded.artifact_id,"
                "snapshot_event_id=excluded.snapshot_event_id,"
                "snapshot_observed_at=excluded.snapshot_observed_at,"
                "current_event_id=excluded.current_event_id,"
                "seen_artifact_id=excluded.seen_artifact_id,prunable=0",
                (fault_key, producer, component, symptom, occurrence, active, 1,
                 inserted_artifact or last_claimed_artifact, None, None,
                 inserted_event_id or (
                     prior_fault["current_event_id"]
                     if prior_fault and same_occurrence and status == (
                         "ACTIVE" if was_active else "INACTIVE"
                     ) else None
                 ), artifact_id, 0),
            )
            self._set_meta("checkpoint", stream_id)
            self._prune()
            return True

    def reconcile_snapshot(self, rows):
        """Record current observations without inventing missing transitions.

        A new explicit row can supply one current-observation event, tagged
        ``source=snapshot``.  It does not assert when the transition happened,
        and a subsequent matching stream record does not create another event.
        Missing rows are ignored: they may have expired or Redis may have lost
        data.  Snapshot reconciliation never advances the stream checkpoint.
        """
        normalized = []
        for row in rows:
            if row.get("producer") != "dldd":
                continue
            component = _text(row.get("component") or row.get("component_name"), "component")
            symptom = _text(row["symptom"], "symptom")
            status = _text(row["status"], "status").upper()
            if status not in ("ACTIVE", "INACTIVE"):
                raise ValueError("invalid fault status")
            occurrence = int(row.get("occurrence", row.get("occurrences", 1)))
            if occurrence < 1:
                raise ValueError("invalid occurrence")
            observed = None
            source_observed_at = None
            if status == "ACTIVE":
                observed = _nanoseconds(row.get("last_detection_time"))
                if row.get("last_detection_time") not in (None, ""):
                    source_observed_at = _seconds(row["last_detection_time"])
            elif row.get("inactive_since") not in (None, ""):
                source_observed_at = _seconds(row["inactive_since"])
            component_type = row.get("component_type") or None
            if component_type is not None:
                component_type = _text(component_type, "component_type")
            normalized.append((
                _fault_key(row, "dldd", component, symptom), component, symptom,
                status, occurrence, observed, component_type,
                source_observed_at, _archive_from_snapshot(row),
            ))

        with self._transaction():
            affected = set()
            active_observed = {}
            inactive_candidates = {}
            reconciliation_at = int(time.time())
            # The caller supplies a complete FAULT_INFO snapshot.  An inactive
            # episode absent from it may be discarded only after its retained
            # event has also aged out; active memberships are never inferred
            # clear from absence.
            self._db.execute("UPDATE faults SET prunable=1 WHERE active=0")
            for (fault_key, component, symptom, status, occurrence, observed,
                 component_type, source_observed_at, archive_id) in normalized:
                prior = self._db.execute(
                    "SELECT * FROM faults WHERE fault_key=?", (fault_key,)
                ).fetchone()
                active = int(status == "ACTIVE")
                changed = (
                    prior is None or prior["active"] != active
                    or prior["occurrence"] != occurrence
                )
                stream_seen = 0 if changed else prior["stream_seen"]
                snapshot_event_id = (
                    prior["snapshot_event_id"] if prior and not changed else None
                )
                snapshot_observed_at = (
                    prior["snapshot_observed_at"] if prior and not changed else None
                )
                current_event_id = (
                    prior["current_event_id"] if prior and not changed else None
                )
                seen_artifact_id = (
                    prior["seen_artifact_id"] if prior and not changed else None
                )
                claimed_artifact = None
                if changed and status == "ACTIVE":
                    snapshot_observed_at = source_observed_at or reconciliation_at
                    snapshot_event_id, claimed_artifact = self._insert_event(
                        f"snapshot:{uuid.uuid4().hex}", component,
                        component_type, symptom, "UNHEALTHY",
                        snapshot_observed_at, archive_id, source="snapshot",
                    )
                    current_event_id = snapshot_event_id
                    seen_artifact_id = archive_id
                elif changed and status == "INACTIVE":
                    inactive_candidates[component] = (
                        fault_key, symptom, component_type,
                        source_observed_at or reconciliation_at, archive_id,
                    )
                    seen_artifact_id = archive_id
                elif archive_id and archive_id != seen_artifact_id:
                    # A metadata-only refresh may supply a *new* archive after
                    # the transition event was published.  Keep its event ID
                    # stable, and attach only to that episode's current event.
                    event = self._db.execute(
                        "SELECT artifact_id FROM events WHERE event_id=? "
                        "AND component=? AND symptom=? AND status=?",
                        (current_event_id, component, symptom,
                         "UNHEALTHY" if active else "HEALTHY"),
                    ).fetchone()
                    claimed = self._db.execute(
                        "SELECT 1 FROM artifact_claims WHERE artifact_id=?",
                        (archive_id,),
                    ).fetchone()
                    if event and event["artifact_id"] is None and not claimed:
                        self._db.execute(
                            "INSERT INTO artifact_claims(artifact_id) VALUES(?)",
                            (archive_id,),
                        )
                        self._db.execute(
                            "UPDATE events SET artifact_id=? WHERE event_id=?",
                            (archive_id, current_event_id),
                        )
                        claimed_artifact = archive_id
                    seen_artifact_id = archive_id
                self._db.execute(
                    "INSERT INTO faults(fault_key,producer,component,symptom,occurrence,"
                    "active,stream_seen,artifact_id,snapshot_event_id,"
                    "snapshot_observed_at,current_event_id,seen_artifact_id,prunable) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(fault_key) DO UPDATE SET component=excluded.component,"
                    "symptom=excluded.symptom,occurrence=excluded.occurrence,"
                    "active=excluded.active,stream_seen=excluded.stream_seen,"
                    "artifact_id=excluded.artifact_id,"
                    "snapshot_event_id=excluded.snapshot_event_id,"
                    "snapshot_observed_at=excluded.snapshot_observed_at,"
                    "current_event_id=excluded.current_event_id,"
                    "seen_artifact_id=excluded.seen_artifact_id,prunable=0",
                    (fault_key, "dldd", component, symptom, occurrence, active,
                     stream_seen, claimed_artifact or (
                         prior["artifact_id"] if prior else None
                     ), snapshot_event_id, snapshot_observed_at,
                     current_event_id, seen_artifact_id, 0),
                )
                affected.add(component)
                if observed is not None:
                    active_observed[component] = max(
                        active_observed.get(component, 0), observed
                    )
            for component in affected:
                active = self._active_count(component) > 0
                if not active and component in inactive_candidates:
                    (fault_key, symptom, component_type,
                     snapshot_observed_at, archive_id) = inactive_candidates[component]
                    snapshot_event_id, claimed_artifact = self._insert_event(
                        f"snapshot:{uuid.uuid4().hex}", component,
                        component_type, symptom, "HEALTHY", snapshot_observed_at,
                        archive_id, source="snapshot",
                    )
                    self._db.execute(
                        "UPDATE faults SET snapshot_event_id=?,snapshot_observed_at=?,"
                        "current_event_id=?,artifact_id=COALESCE(?,artifact_id) "
                        "WHERE fault_key=?",
                        (snapshot_event_id, snapshot_observed_at,
                         snapshot_event_id, claimed_artifact, fault_key),
                    )
                prior = self._db.execute(
                    "SELECT * FROM aggregates WHERE component=?", (component,)
                ).fetchone()
                count = prior["unhealthy_count"] if prior else 0
                if active and (prior is None or prior["status"] != "UNHEALTHY"):
                    count += 1
                last = prior["last_unhealthy"] if prior else None
                if component in active_observed:
                    last = max(last or 0, active_observed[component])
                self._db.execute(
                    "INSERT INTO aggregates(component,status,last_unhealthy,unhealthy_count) "
                    "VALUES(?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                    "status=excluded.status,last_unhealthy=excluded.last_unhealthy,"
                    "unhealthy_count=excluded.unhealthy_count",
                    (component, "UNHEALTHY" if active else "HEALTHY", last, count),
                )
            self._prune()

    def get_latest(self, component):
        component = _text(component, "component")
        with self._lock:
            return _event(self._db.execute(
                "SELECT * FROM events WHERE component=? ORDER BY seq DESC LIMIT 1",
                (component,),
            ).fetchone())

    def list_events(self, component=None, include_acknowledged=False):
        conditions = []
        parameters = []
        if component is not None:
            conditions.append("component=?")
            parameters.append(_text(component, "component"))
        if not include_acknowledged:
            conditions.append("acknowledged=0")
        query = "SELECT * FROM events"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY seq DESC"
        with self._lock:
            return [_event(row) for row in self._db.execute(query, parameters)]

    def acknowledge(self, component, event_id):
        component = _text(component, "component")
        event_id = _text(event_id, "event_id")
        with self._transaction():
            self._db.execute(
                "UPDATE events SET acknowledged=1 WHERE component=? AND event_id=?",
                (component, event_id),
            )
            return _event(self._db.execute(
                "SELECT * FROM events WHERE component=? AND event_id=?",
                (component, event_id),
            ).fetchone())

    def get_aggregate(self, component):
        component = _text(component, "component")
        with self._lock:
            return _aggregate(self._db.execute(
                "SELECT * FROM aggregates WHERE component=?", (component,)
            ).fetchone())

    def list_aggregates(self):
        with self._lock:
            return [_aggregate(row) for row in self._db.execute(
                "SELECT * FROM aggregates ORDER BY component"
            )]
