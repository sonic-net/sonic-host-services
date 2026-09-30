"""Durable Healthz metadata and STATE_DB projection on the host.

The background worker is the only Redis consumer.  D-Bus methods read the
SQLite catalog and never wait for a fault sample or diagnostic collection.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import threading
import time
from urllib.parse import quote, unquote

from host_modules import host_service
from host_modules.healthz_artifacts import (
    ARTIFACT_DIRECTORY, ARTIFACT_NAME, MAX_REQUEST_BYTES, HealthzArtifacts,
)
from host_modules.healthz_catalog import HealthzCatalog

LOGGER = logging.getLogger(__name__)
MOD_NAME = "healthz"
TRANSITION_STREAM = "HEALTHZ_TRANSITIONS"
HEALTH_PREFIX = "COMPONENT_HEALTH_INFO|"
ENTITY_PREFIX = "PHYSICAL_ENTITY_INFO|"
LEGACY_ARTIFACT_DIRECTORY = "/var/lib/sonic/dldd/artifacts"
_LEGACY_ARTIFACT_NAME = re.compile(r"^dldd-[0-9a-f]{32}\.tar\.gz$")
_POLL_SECONDS = 1
_RECONCILE_SECONDS = 30
_MAX_REQUEST_BYTES = 4096


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _stream_id(value):
    parts = _text(value).split("-", 1)
    if len(parts) != 2:
        raise ValueError("invalid Redis stream ID")
    return int(parts[0]), int(parts[1])


class HealthzRedis:
    """Small STATE_DB Redis boundary, with a reconnectable production client."""

    def __init__(self, client=None):
        self._client = client
        self._injected = client is not None

    def client(self):
        if self._client is None:
            import redis
            from swsscommon import swsscommon

            database = "STATE_DB"
            key = swsscommon.SonicDBKey()
            options = {"db": swsscommon.SonicDBConfig.getDbId(database, key),
                       "socket_timeout": 3, "socket_connect_timeout": 3}
            socket = swsscommon.SonicDBConfig.getDbSock(database, key)
            if socket:
                options["unix_socket_path"] = socket
            else:
                options["host"] = swsscommon.SonicDBConfig.getDbHostname(database, key)
                options["port"] = swsscommon.SonicDBConfig.getDbPort(database, key)
            self._client = redis.Redis(**options)
        return self._client

    def disconnect(self):
        if self._client is not None and not self._injected:
            self._client.connection_pool.disconnect()
            self._client = None


class ParentRelations:
    """Published PHYSICAL_ENTITY_INFO ancestry cached off the D-Bus path."""

    def __init__(self):
        self._lock = threading.RLock()
        self._parents = {}

    def replace(self, parents):
        with self._lock:
            self._parents = dict(parents)

    def children(self, component):
        with self._lock:
            return sorted(
                child for child, parent in self._parents.items()
                if parent == component
            )


class HealthzWorker:
    """Consume generic transitions and project component health."""

    def __init__(self, catalog, state_db=None, relations=None,
                 reconcile_seconds=_RECONCILE_SECONDS):
        self.catalog = catalog
        self.state_db = state_db or HealthzRedis()
        self.relations = relations or ParentRelations()
        self.reconcile_seconds = reconcile_seconds
        self._next_reconcile = 0
        self._pending_projection = {
            row["component"] for row in self.catalog.list_aggregates()
        }
        self._last_gap = None
        self._run_id_unavailable_logged = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self.run, name="healthz-transitions",
                                            daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.state_db.disconnect()

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_once(block_ms=1000)
            except Exception:
                LOGGER.exception("Healthz transition processing failed; retrying")
                self.state_db.disconnect()
                self._next_reconcile = 0
                self._stop.wait(_POLL_SECONDS)

    def poll_once(self, block_ms=None):
        """One bounded replay, parent refresh, and projection pass."""

        client = self.state_db.client()
        self._check_redis_run_id(client)
        checkpoint = self.catalog.get_checkpoint()
        first = client.xrange(TRANSITION_STREAM, count=1)
        last = client.xrevrange(TRANSITION_STREAM, count=1)
        if not checkpoint:
            self._check_initial_stream_trim(client, first)
        if checkpoint and checkpoint != "0-0":
            if not last or _stream_id(checkpoint) > _stream_id(last[0][0]):
                # A retained prefix can contain transitions already superseded
                # in SQLite.  Replaying it would roll back known source state.
                resume_after_id = _text(last[0][0]) if last else "0-0"
                self._report_gap("transition stream was lost or reset", checkpoint,
                                 _text(first[0][0]) if first else None,
                                 resume_after_id=resume_after_id)
                checkpoint = resume_after_id
            elif first and _stream_id(checkpoint) < _stream_id(first[0][0]):
                # A trimmed checkpoint can also mean all trimmed entries were
                # consumed.  Redis does not retain enough information to prove
                # continuity, so report this as a possible history gap.
                self._report_gap("checkpoint precedes first retained transition",
                                 checkpoint, _text(first[0][0]))
        kwargs = {"count": 128}
        if block_ms is not None:
            kwargs["block"] = block_ms
        batches = client.xread({TRANSITION_STREAM: checkpoint or "0-0"}, **kwargs)
        if batches and not checkpoint:
            # The stream can be trimmed while the initial XREAD is blocked.
            self._check_initial_stream_trim(
                client, client.xrange(TRANSITION_STREAM, count=1)
            )
        if batches and checkpoint and checkpoint != "0-0":
            # Trimming can advance the head while XREAD is blocked, after the
            # first bounds check.  Record the possible loss before committing
            # any returned transition to the catalog.
            first = client.xrange(TRANSITION_STREAM, count=1)
            if first and _stream_id(checkpoint) < _stream_id(first[0][0]):
                self._report_gap("checkpoint precedes first retained transition",
                                 checkpoint, _text(first[0][0]))
        for _, entries in batches:
            for stream_id, raw in entries:
                transition = {_text(name): _text(value) for name, value in raw.items()}
                sid = _text(stream_id)
                try:
                    consumed_new = self.catalog.apply_transition(sid, transition)
                except (KeyError, TypeError, ValueError) as error:
                    self._report_gap(f"invalid transition record: {error}",
                                     sid, sid, resume_after_id=sid)
                    continue
                if consumed_new:
                    self._pending_projection.add(transition["component"])

        if time.monotonic() >= self._next_reconcile:
            self._refresh_parents(client)
            self._next_reconcile = time.monotonic() + self.reconcile_seconds
        self._project_pending(client)

    def _check_initial_stream_trim(self, client, first):
        """Report provable pre-checkpoint loss without inventing old events."""
        if self.catalog.get_gap() is not None:
            return
        try:
            info = client.xinfo_stream(TRANSITION_STREAM)
            deleted_id = info.get("max-deleted-entry-id",
                                  info.get(b"max-deleted-entry-id"))
            trimmed = deleted_id is not None and _stream_id(deleted_id) > (0, 0)
            if not trimmed:
                added = info.get("entries-added", info.get(b"entries-added"))
                length = info.get("length", info.get(b"length"))
                trimmed = (added is not None and length is not None
                           and int(added) > int(length))
            # Redis before 7 lacks these counters, so its initial trimmed
            # prefix cannot be established without a prior checkpoint.
        except Exception as error:  # noqa: BLE001 - optional Redis XINFO metadata
            LOGGER.debug("Healthz initial stream trim metadata unavailable: %s", error)
            return
        if trimmed:
            self._report_gap("transition stream was trimmed before first checkpoint",
                             None, _text(first[0][0]) if first else None)

    def _check_redis_run_id(self, client):
        try:
            info = client.info("server")
            run_id = info.get("run_id")
        except Exception as error:  # noqa: BLE001 - optional Redis INFO across client versions
            if not self._run_id_unavailable_logged:
                LOGGER.warning("Redis run_id unavailable; restart-only stream loss "
                               "cannot be detected: %s", error)
                self._run_id_unavailable_logged = True
            return
        if not run_id:
            if not self._run_id_unavailable_logged:
                LOGGER.warning("Redis INFO server has no run_id; restart-only "
                               "stream loss cannot be detected")
                self._run_id_unavailable_logged = True
            return
        run_id = _text(run_id)
        previous = self.catalog.get_redis_run_id()
        if previous == run_id:
            return
        if previous is not None:
            # Redis can restart with the same stream bounds even when an
            # unconsumed tail was lost.  This is a conservative possible-gap
            # signal; the catalog cannot reconstruct an unretained event.
            self._last_gap = None
            self._report_gap("Redis run_id changed; possible unconsumed "
                             "transition loss", self.catalog.get_checkpoint(), None)
            self._next_reconcile = 0
        # The gap marker is persisted before the new epoch marker.  A crash
        # between them can only repeat the warning, not hide potential loss.
        self.catalog.set_redis_run_id(run_id)

    def _report_gap(self, reason, checkpoint, first_available_id,
                    resume_after_id=None):
        signature = (reason, checkpoint, first_available_id)
        if signature != self._last_gap:
            LOGGER.error("Healthz transition history gap: %s; checkpoint=%s first=%s",
                         reason, checkpoint, first_available_id)
            self.catalog.mark_gap(reason, first_available_id,
                                  resume_after_id=resume_after_id)
            self._pending_projection.update(
                row["component"] for row in self.catalog.list_aggregates()
            )
            self._last_gap = signature

    def _refresh_parents(self, client):
        parents = {}
        for raw_key in client.scan_iter(match=ENTITY_PREFIX + "*"):
            key = _text(raw_key)
            row = {_text(k): _text(v) for k, v in client.hgetall(raw_key).items()}
            parent = row.get("parent_name")
            if parent:
                parents[unquote(key[len(ENTITY_PREFIX):])] = parent
        self.relations.replace(parents)

    def _project_pending(self, client):
        for component in tuple(sorted(self._pending_projection)):
            aggregate = self.catalog.get_aggregate(component)
            if aggregate is None:
                self._pending_projection.discard(component)
                continue
            key = HEALTH_PREFIX + quote(component, safe="")
            values = {
                "status": aggregate["status"],
                "unhealthy_count": str(aggregate["unhealthy_count"]),
            }
            if aggregate.get("last_unhealthy") is not None:
                values["last_unhealthy"] = str(aggregate["last_unhealthy"])
            transaction = client.pipeline(transaction=True)
            transaction.hset(key, mapping=values)
            if "last_unhealthy" not in values:
                transaction.hdel(key, "last_unhealthy")
            transaction.execute()
            self._pending_projection.discard(component)


class Healthz(host_service.HostModule):
    """Quick JSON metadata D-Bus API for gNOI Healthz."""

    def __init__(self, mod_name=MOD_NAME, catalog=None, state_db=None,
                 artifact_directory=ARTIFACT_DIRECTORY, start_worker=True,
                 legacy_artifact_directory=None):
        super().__init__(mod_name)
        self.catalog = catalog or HealthzCatalog()
        self.artifact_directory = artifact_directory
        self.artifacts = HealthzArtifacts(artifact_directory)
        self.legacy_artifact_directory = (legacy_artifact_directory or
            (LEGACY_ARTIFACT_DIRECTORY if artifact_directory == ARTIFACT_DIRECTORY
             else artifact_directory))
        self.relations = ParentRelations()
        self.worker = HealthzWorker(self.catalog, state_db, self.relations)
        if start_worker:
            self.worker.start()

    def shutdown(self):
        self.worker.stop()

    @staticmethod
    def _request(raw, require_id=False, require_component=True):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ValueError("invalid Healthz request")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("Healthz request must be an object")
        component = request.get("component", "")
        if (not isinstance(component, str) or "\x00" in component
                or (require_component and not component)):
            raise ValueError("component must be a valid string")
        if require_id:
            event_id = request.get("id")
            if not isinstance(event_id, str) or not event_id or "\x00" in event_id:
                raise ValueError("id must be a nonempty string")
        return request

    def _visible(self, event):
        event = dict(event)
        artifact_id = event.get("artifact_id")
        if artifact_id and self._artifact_available(artifact_id):
            return event
        event.pop("artifact_id", None)
        return event

    def _artifact_available(self, artifact_id):
        if not isinstance(artifact_id, str):
            return False
        if ARTIFACT_NAME.fullmatch(artifact_id):
            return self.artifacts.status(artifact_id) == "COMPLETED"
        if not _LEGACY_ARTIFACT_NAME.fullmatch(artifact_id):
            return False
        try:
            directory = os.lstat(self.legacy_artifact_directory)
            if not stat.S_ISDIR(directory.st_mode):
                return False
            archive = os.stat(os.path.join(self.legacy_artifact_directory, artifact_id),
                              follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(archive.st_mode)

    def _status_tree(self, component, visited):
        if component in visited:
            return None
        branch = visited | {component}
        children = []
        for child in self.relations.children(component):
            child_status = self._status_tree(child, branch)
            if child_status is not None:
                children.append(child_status)
        event = self.catalog.get_latest(component)
        if event is None and not children:
            return None
        result = (self._visible(event) if event is not None else {
            "component": component, "status": "UNSPECIFIED",
        })
        if children:
            result["children"] = children
        return result

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def get(self, raw):
        try:
            component = self._request(raw)["component"]
            result = self._status_tree(component, set())
            if result is None:
                return errno.ENOENT, "Healthz event not found"
            return 0, json.dumps(result, separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz get failed")
            return errno.EIO, "Healthz catalog read failed"

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def list(self, raw):
        try:
            request = self._request(raw, require_component=False)
            include = request.get("include_acknowledged", False)
            if not isinstance(include, bool):
                raise TypeError("include_acknowledged must be boolean")
            rows = self.catalog.list_events(request.get("component") or None, include)
            return 0, json.dumps([self._visible(row) for row in rows],
                                 separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz list failed")
            return errno.EIO, "Healthz catalog read failed"

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def ack(self, raw):
        try:
            request = self._request(raw, require_id=True)
            event = self.catalog.acknowledge(request["component"], request["id"])
            if event is None:
                return errno.ENOENT, "Healthz event not found"
            return 0, json.dumps(self._visible(event), separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz acknowledgement failed")
            return errno.EIO, "Healthz catalog update failed"

    @staticmethod
    def _artifact_request(raw):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("invalid Healthz artifact request")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("Healthz artifact request must be an object")
        return request

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def reserve_artifact(self, raw):
        try:
            if self._artifact_request(raw):
                raise ValueError("reserve_artifact takes no options")
            return 0, json.dumps(self.artifacts.reserve(), separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz artifact reservation failed")
            return errno.EIO, "Healthz artifact reservation failed"

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def submit_artifact(self, raw):
        try:
            request = self._artifact_request(raw)
            result = self.artifacts.submit(request.get("artifact_id"),
                                           request.get("paths"),
                                           request.get("metadata"))
            return 0, json.dumps(result, separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except FileNotFoundError as error:
            return errno.ENOENT, str(error)
        except Exception:
            LOGGER.exception("Healthz artifact submission failed")
            return errno.EIO, "Healthz artifact submission failed"

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def artifact_status(self, raw):
        try:
            artifact_id = self._artifact_request(raw).get("artifact_id")
            if _LEGACY_ARTIFACT_NAME.fullmatch(artifact_id or ""):
                state = "COMPLETED" if self._artifact_available(artifact_id) else "MISSING"
            else:
                state = self.artifacts.status(artifact_id)
            return 0, json.dumps({"state": state}, separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz artifact status failed")
            return errno.EIO, "Healthz artifact status failed"

    @host_service.method(host_service.bus_name(MOD_NAME),
                         in_signature="s", out_signature="is")
    def fail_artifact(self, raw):
        try:
            artifact_id = self._artifact_request(raw).get("artifact_id")
            self.artifacts.fail(artifact_id)
            return 0, json.dumps({"state": self.artifacts.status(artifact_id)},
                                 separators=(",", ":"))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            return errno.EINVAL, str(error)
        except Exception:
            LOGGER.exception("Healthz artifact failure update failed")
            return errno.EIO, "Healthz artifact failure update failed"
