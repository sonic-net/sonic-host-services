"""Durable Healthz events, source membership, and component assessments.

Producers send generic transitions.  The catalog never reads a producer's
telemetry rows; it commits each transition and its stream checkpoint together.
"""

from __future__ import annotations

import math
import logging
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager

DEFAULT_CATALOG_PATH = "/var/lib/sonic/healthz/catalog.sqlite3"
DEFAULT_MAX_EVENTS = 4096
MAX_CATALOG_BYTES = 32 * 1024 * 1024
DEFAULT_SOURCE_RETAIN_SECONDS = 24 * 60 * 60
SOURCE_PRUNE_GRACE_SECONDS = 60
_MAX_SECONDS = ((1 << 63) - 1) // 1000000000
_CHECKPOINT = "healthz_checkpoint"
LOGGER = logging.getLogger(__name__)


def _text(value, name):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(f"{name} must be nonempty text of at most 1024 characters")
    return value


def _stream_parts(stream_id):
    parts = _text(stream_id, "stream_id").split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("invalid Redis stream ID")
    return int(parts[0]), int(parts[1])


def _seconds(value):
    if isinstance(value, bool):
        raise ValueError("invalid observation timestamp")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("invalid observation timestamp")
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid observation timestamp")
    seconds = math.floor(number)
    if seconds > _MAX_SECONDS:
        raise ValueError("invalid observation timestamp")
    return seconds


def _active(value):
    if value in ("1", 1, True):
        return 1
    if value in ("0", 0, False):
        return 0
    raise ValueError("active must be 0 or 1")


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
    """Thread-safe SQLite catalog shared by the host worker and D-Bus RPCs."""

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
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
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
                f"PRAGMA max_page_count={max_pages}"
            ).fetchone()[0]
            if actual_max > max_pages:
                raise RuntimeError("existing Healthz catalog exceeds the 32 MiB limit")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=2000")
            self._init_schema()

    def _init_schema(self):
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
            CREATE TABLE IF NOT EXISTS sources (
                producer TEXT NOT NULL,
                source_key TEXT NOT NULL,
                component TEXT NOT NULL,
                symptom TEXT NOT NULL,
                active INTEGER NOT NULL,
                last_transition_id TEXT,
                last_artifact_id TEXT,
                retain_until INTEGER,
                legacy INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(producer, source_key)
            );
            CREATE INDEX IF NOT EXISTS sources_component_active
                ON sources(component, active);
            CREATE TABLE IF NOT EXISTS aggregates (
                component TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('HEALTHY', 'UNHEALTHY')),
                last_unhealthy INTEGER,
                unhealthy_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS metadata (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Keep events and acknowledgements from the previous DLDD-specific
        # catalog.  Its current fault membership seeds the generic source table.
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(events)")}
        if "source" not in columns:
            self._db.execute(
                "ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'stream'"
            )
        if self._meta("sources_migrated") is None:
            old = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='faults'"
            ).fetchone()
            if old:
                self._db.execute(
                    "INSERT OR IGNORE INTO sources(producer,source_key,component,"
                    "symptom,active,last_artifact_id,legacy) "
                    "SELECT producer,fault_key,component,symptom,active,artifact_id,1 "
                    "FROM faults"
                )
            if self._meta("checkpoint") and self._meta(_CHECKPOINT) is None:
                # The old stream may have had an unconsumed tail.  Keep its
                # retained events but do not claim that tail was replayed.
                reason = "legacy DLDD transition stream tail not replayed"
                previous = self._meta("gap_reason")
                if previous:
                    reason += f"; prior gap: {previous}"
                self._set_meta("gap_reason", reason[:512])
                self._set_meta("gap_recorded_at", str(int(time.time())))
                self._db.execute(
                    "DELETE FROM metadata WHERE name='gap_first_available_id'"
                )
            self._set_meta("sources_migrated", "1")

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
            return self._meta(_CHECKPOINT)

    def get_redis_run_id(self):
        with self._lock:
            return self._meta("redis_run_id")

    def set_redis_run_id(self, run_id):
        run_id = _text(run_id, "redis_run_id")
        if len(run_id) > 128:
            raise ValueError("redis_run_id is too long")
        with self._transaction():
            self._set_meta("redis_run_id", run_id)

    def set_checkpoint(self, stream_id):
        """Advance past a known stream position without making an event."""
        _stream_parts(stream_id)
        with self._transaction():
            previous = self._meta(_CHECKPOINT)
            if previous and _stream_parts(stream_id) < _stream_parts(previous):
                raise ValueError("checkpoint rewind requires mark_gap")
            self._set_meta(_CHECKPOINT, stream_id)

    def mark_gap(self, reason, first_available_id=None, resume_after_id=None):
        """Record known or possible stream loss without inventing an event."""
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
            else:
                self._db.execute(
                    "DELETE FROM metadata WHERE name='gap_first_available_id'"
                )
            if resume_after_id is not None:
                self._set_meta(_CHECKPOINT, resume_after_id)

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
            "SELECT COUNT(*) AS total FROM sources WHERE component=? AND active=1",
            (component,),
        ).fetchone()["total"]

    def _insert_event(self, stream_id, component, component_type, symptom,
                      status, observed_at, artifact_id, source):
        if artifact_id:
            prior = self._db.execute(
                "SELECT 1 FROM events WHERE event_id=? OR artifact_id=?",
                (artifact_id, artifact_id),
            ).fetchone()
            claimed = self._db.execute(
                "SELECT 1 FROM sources WHERE last_artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if prior or claimed:
                LOGGER.warning("Healthz artifact ID already used: %s", artifact_id)
                artifact_id = None
        event_id = artifact_id or f"hz-{uuid.uuid4().hex}"
        self._db.execute(
            "INSERT INTO events(event_id,stream_id,component,component_type,"
            "symptom,status,observed_at,artifact_id,source) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, stream_id, component, component_type, symptom,
             status, observed_at, artifact_id, source),
        )
        return artifact_id

    def _prune(self):
        count = self._db.execute("SELECT COUNT(*) AS total FROM events").fetchone()["total"]
        excess = count - self.max_events
        if excess > 0:
            # Keep a component's newest event before its older events.
            self._db.execute(
                "DELETE FROM events WHERE seq IN ("
                "SELECT e.seq FROM events e ORDER BY "
                "e.seq=(SELECT MAX(latest.seq) FROM events latest "
                "WHERE latest.component=e.component), "
                "e.acknowledged DESC, e.seq ASC LIMIT ?)",
                (excess,),
            )
        # An inactive source's transition ID must survive as long as the
        # producer retains its current row.  Allow for rounded Redis TTLs.
        self._db.execute(
            "DELETE FROM sources WHERE active=0 AND retain_until IS NOT NULL "
            "AND retain_until<=?", (int(time.time()) - SOURCE_PRUNE_GRACE_SECONDS,),
        )

    def apply_transition(self, stream_id, transition):
        """Apply one generic transition; return False for an idempotent replay."""
        _stream_parts(stream_id)
        if transition.get("kind") == "observation":
            producer = _text(transition["producer"], "producer")
            source_key = _text(transition["source_key"], "source_key")
            component = _text(transition["component"], "component")
            observed_at = _seconds(transition["observed_at"])
            with self._transaction():
                checkpoint = self._meta(_CHECKPOINT)
                if checkpoint and _stream_parts(stream_id) <= _stream_parts(checkpoint):
                    return False
                updated = self._db.execute(
                    "UPDATE aggregates SET last_unhealthy=MAX("
                    "COALESCE(last_unhealthy,0),?) "
                    "WHERE component=? AND status='UNHEALTHY' "
                    "AND COALESCE(last_unhealthy,0)<? "
                    "AND EXISTS (SELECT 1 FROM sources s WHERE s.producer=? "
                    "AND s.source_key=? AND s.component=aggregates.component "
                    "AND s.active=1)",
                    (observed_at * 1000000000, component,
                     observed_at * 1000000000, producer, source_key),
                ).rowcount
                self._set_meta(_CHECKPOINT, stream_id)
                return bool(updated)
        producer = _text(transition["producer"], "producer")
        source_key = _text(transition["source_key"], "source_key")
        transition_id = _text(transition["transition_id"], "transition_id")
        component = _text(transition["component"], "component")
        symptom = _text(transition["symptom"], "symptom")
        component_type = transition.get("component_type") or None
        if component_type is not None:
            component_type = _text(component_type, "component_type")
        active = _active(transition["active"])
        observed_at = _seconds(transition["observed_at"])
        last_unhealthy_at = transition.get("last_unhealthy_at")
        if last_unhealthy_at not in (None, ""):
            last_unhealthy_at = _seconds(last_unhealthy_at)
            if not last_unhealthy_at:
                raise ValueError("last_unhealthy_at must be positive")
        else:
            last_unhealthy_at = None
        artifact_id = transition.get("artifact_id") or None
        if artifact_id is not None:
            artifact_id = _text(artifact_id, "artifact_id")
        retain_until = transition.get("retain_until")
        if retain_until not in (None, ""):
            retain_until = _seconds(retain_until)
        else:
            retain_until = None
        if not active and retain_until is None:
            retain_until = max(observed_at, int(time.time())) + DEFAULT_SOURCE_RETAIN_SECONDS
        replay = _active(transition.get("replay", "0"))

        with self._transaction():
            checkpoint = self._meta(_CHECKPOINT)
            if checkpoint and _stream_parts(stream_id) <= _stream_parts(checkpoint):
                return False
            prior = self._db.execute(
                "SELECT * FROM sources WHERE producer=? AND source_key=?",
                (producer, source_key),
            ).fetchone()
            if prior and prior["component"] != component:
                raise ValueError("Healthz source changed component")
            if prior and prior["last_transition_id"] == transition_id:
                if prior["active"] != active:
                    raise ValueError("Healthz transition ID changed status")
                self._set_meta(_CHECKPOINT, stream_id)
                return False

            old_count = self._active_count(component)
            was_active = bool(prior["active"]) if prior else False
            aggregate = self._db.execute(
                "SELECT * FROM aggregates WHERE component=?", (component,)
            ).fetchone()
            # An existing DUT catalog predates producer transition IDs.  Its
            # first matching DLDD replay seeds the ID without duplicating an
            # already retained event or incrementing unhealthy-count.
            migrated_replay = bool(
                prior and prior["legacy"] and replay and prior["active"] == active
                and aggregate is not None
                and aggregate["status"] == ("UNHEALTHY" if active else "HEALTHY")
                and (artifact_id is None or artifact_id == prior["last_artifact_id"])
            )
            new_count = old_count - was_active + active
            event_status = None
            if not migrated_replay:
                if active:
                    event_status = "UNHEALTHY"
                elif new_count == 0:
                    event_status = "HEALTHY"
                elif artifact_id and (
                    prior is None or artifact_id != prior["last_artifact_id"]
                ):
                    # A locally recovered source can have a new archive even
                    # while another source keeps the component unhealthy.
                    event_status = "UNHEALTHY"
            claimed_artifact = None
            if event_status:
                claimed_artifact = self._insert_event(
                    stream_id, component, component_type, symptom, event_status,
                    observed_at, artifact_id, "replay" if replay else "stream",
                )
            self._db.execute(
                "INSERT INTO sources(producer,source_key,component,symptom,active,"
                "last_transition_id,last_artifact_id,retain_until,legacy) "
                "VALUES(?,?,?,?,?,?,?,?,0) ON CONFLICT(producer,source_key) DO UPDATE SET "
                "component=excluded.component,symptom=excluded.symptom,"
                "active=excluded.active,last_transition_id=excluded.last_transition_id,"
                "last_artifact_id=excluded.last_artifact_id,"
                "retain_until=excluded.retain_until,legacy=0",
                (producer, source_key, component, symptom, active, transition_id,
                 claimed_artifact or (prior["last_artifact_id"] if prior else None),
                 retain_until),
            )
            count = aggregate["unhealthy_count"] if aggregate else 0
            if active and not migrated_replay and (
                old_count == 0 or aggregate is None
                or aggregate["status"] != "UNHEALTHY"
            ):
                count += 1
            last = aggregate["last_unhealthy"] if aggregate else None
            if active:
                last = max(last or 0, observed_at * 1000000000)
            if last_unhealthy_at is not None:
                last = max(last or 0, last_unhealthy_at * 1000000000)
            self._db.execute(
                "INSERT INTO aggregates(component,status,last_unhealthy,unhealthy_count) "
                "VALUES(?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                "status=excluded.status,last_unhealthy=excluded.last_unhealthy,"
                "unhealthy_count=excluded.unhealthy_count",
                (component, "UNHEALTHY" if new_count else "HEALTHY", last, count),
            )
            self._set_meta(_CHECKPOINT, stream_id)
            self._prune()
            return True

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

    def acknowledged_artifacts(self):
        """Archive IDs eligible for preferred eviction; retain their events."""
        with self._lock:
            return {row["artifact_id"] for row in self._db.execute(
                "SELECT artifact_id FROM events "
                "WHERE acknowledged=1 AND artifact_id IS NOT NULL"
            )}

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
