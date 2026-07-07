"""Asynchronous Healthz artifact production boundary."""

from __future__ import annotations

import glob
import io
import json
import logging
import os
import re
import stat
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from queue import Full, Queue
from typing import Any, Callable, Iterable, Mapping, Optional

from .lifecycle import _atomic_json
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIRECTORY = "/var/lib/sonic/dldd/artifacts"
_ARTIFACT_BASE_PATTERN = re.compile(r"^dldd-[0-9a-f]{32}$")
_STAGED_ARCHIVE_PATTERN = re.compile(
    r"^\.dldd-[0-9a-f]{32}-[A-Za-z0-9_-]+\.tar\.gz$"
)


@dataclass(frozen=True)
class ArtifactRequest:
    artifact_id: str
    state: str
    requested_at: float
    completed_at: Optional[float] = None
    last_error: str = ""

    def as_payload(self) -> Mapping[str, Any]:
        return floor_timestamp_fields(
            {
                "artifact_id": self.artifact_id,
                "state": self.state,
                "requested_at": self.requested_at,
                "completed_at": self.completed_at,
                "last_error": self.last_error,
            }
        )


class HealthzArtifactClient:
    def request(
        self,
        metadata: Mapping[str, Any],
        logs: Iterable[str],
        queries: Iterable[Mapping[str, Any]],
    ) -> ArtifactRequest:
        raise NotImplementedError

    def status(self, artifact_id: str) -> ArtifactRequest:
        raise NotImplementedError

    def shutdown(self, wait: bool = True) -> None:
        """Release worker resources owned by the artifact implementation."""


class FilesystemArtifactClient(HealthzArtifactClient):
    """Produce artifacts in a directory exported by the gNOI Healthz server."""

    def __init__(
        self,
        directory: str = DEFAULT_ARTIFACT_DIRECTORY,
        query_runner: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        max_workers: int = 2,
        max_artifacts: int = 20,
        max_artifact_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.directory = directory
        self.query_runner = query_runner or self._run_query
        self.max_artifacts = max(1, max_artifacts)
        self.max_artifact_bytes = max(1024, max_artifact_bytes)
        max_workers = max(1, int(max_workers))
        self._jobs = Queue(maxsize=self.max_artifacts)
        self._query_slots = threading.BoundedSemaphore(max_workers)
        self._store_lock = threading.RLock()
        self._active = set()
        self._closed = False
        os.makedirs(self.directory, mode=0o750, exist_ok=True)
        with self._store_lock:
            self._reconcile_store_locked()
            self._prune_locked()
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                name="dldd-artifacts-{}".format(index),
                daemon=True,
            )
            for index in range(max_workers)
        )
        for worker in self._workers:
            worker.start()

    def request(
        self,
        metadata: Mapping[str, Any],
        logs: Iterable[str],
        queries: Iterable[Mapping[str, Any]],
    ) -> ArtifactRequest:
        requested = time.time()
        artifact_base = "dldd-{}".format(uuid.uuid4().hex)
        artifact_id = artifact_base + ".tar.gz"
        state_path = os.path.join(self.directory, artifact_base + ".json")
        request = ArtifactRequest(artifact_id, "REQUESTED", requested)
        job = (
            artifact_base,
            artifact_id,
            requested,
            dict(metadata),
            tuple(logs),
            tuple(queries),
        )
        with self._store_lock:
            if self._closed:
                raise RuntimeError("artifact client is shut down")
            self._prune_locked(reserve=1)
            if self._entry_count_locked() >= self.max_artifacts:
                raise RuntimeError(
                    "artifact store capacity is exhausted by active jobs"
                )
            self._active.add(artifact_base)
            try:
                _atomic_json(state_path, request.as_payload())
                self._jobs.put_nowait(job)
            except Exception:
                self._active.discard(artifact_base)
                self._remove_pair_locked(artifact_base)
                raise
        return request

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                try:
                    self._collect(*job)
                except Exception as error:
                    artifact_base, artifact_id, requested = job[:3]
                    try:
                        self._record_terminal(
                            artifact_base,
                            ArtifactRequest(
                                artifact_id,
                                "FAILED",
                                requested,
                                time.time(),
                                str(error),
                            ),
                        )
                    except Exception:
                        LOGGER.exception(
                            "unable to record failed artifact %s", artifact_id
                        )
            finally:
                self._jobs.task_done()

    def _collect(
        self,
        artifact_base: str,
        artifact_id: str,
        requested: float,
        metadata: Mapping[str, Any],
        logs,
        queries,
    ) -> None:
        archive_path = os.path.join(self.directory, artifact_id)
        staged_archive = None
        try:
            descriptor, staged_archive = tempfile.mkstemp(
                prefix=".{}-".format(artifact_base),
                suffix=".tar.gz",
                dir=self.directory,
            )
            os.close(descriptor)
            self._record_state(
                artifact_base,
                ArtifactRequest(artifact_id, "RUNNING", requested),
            )
            with tarfile.open(staged_archive, "w:gz") as archive:
                bytes_added = 0
                metadata_data = json.dumps(
                    floor_timestamp_fields(metadata),
                    sort_keys=True,
                    indent=2,
                ).encode()
                if len(metadata_data) > self.max_artifact_bytes:
                    raise RuntimeError("artifact metadata exceeds the size limit")
                info = tarfile.TarInfo("metadata.json")
                info.size = len(metadata_data)
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(metadata_data))
                bytes_added += len(metadata_data)
                for pattern in logs:
                    for path in sorted(glob.glob(pattern)):
                        added = self._add_log_file(
                            archive,
                            path,
                            self.max_artifact_bytes - bytes_added,
                        )
                        bytes_added += added
                for index, query in enumerate(queries):
                    output = self._run_bounded_query(query)
                    data = (
                        output
                        if isinstance(output, bytes)
                        else str(output).encode("utf-8", "replace")
                    )
                    data = data[: int(query.get("max_output_bytes", 1024 * 1024))]
                    if bytes_added + len(data) > self.max_artifact_bytes:
                        break
                    info = tarfile.TarInfo("queries/{:03d}.txt".format(index))
                    info.size = len(data)
                    info.mtime = int(time.time())
                    archive.addfile(info, io.BytesIO(data))
                    bytes_added += len(data)
            if os.path.getsize(staged_archive) > self.max_artifact_bytes:
                raise RuntimeError("generated artifact exceeds the size limit")
            with open(staged_archive, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(staged_archive, archive_path)
            self._record_terminal(
                artifact_base,
                ArtifactRequest(
                    artifact_id, "COMPLETED", requested, time.time()
                ),
            )
        except Exception as error:
            if staged_archive is not None:
                try:
                    os.unlink(staged_archive)
                except FileNotFoundError:
                    pass
            self._record_terminal(
                artifact_base,
                ArtifactRequest(
                    artifact_id, "FAILED", requested, time.time(), str(error)
                ),
            )

    def _record_state(self, artifact_base: str, request: ArtifactRequest) -> None:
        with self._store_lock:
            _atomic_json(
                os.path.join(self.directory, artifact_base + ".json"),
                request.as_payload(),
            )

    def _record_terminal(
        self, artifact_base: str, request: ArtifactRequest
    ) -> None:
        with self._store_lock:
            try:
                _atomic_json(
                    os.path.join(self.directory, artifact_base + ".json"),
                    request.as_payload(),
                )
            finally:
                self._active.discard(artifact_base)
                self._prune_locked()

    @staticmethod
    def _add_log_file(archive, path: str, remaining: int) -> int:
        """Add one regular file without following links or recursing."""

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return 0
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            return 0
        with os.fdopen(descriptor, "rb") as stream:
            if file_stat.st_size <= 0 or file_stat.st_size > remaining:
                return 0
            info = tarfile.TarInfo(
                os.path.join("logs", os.path.basename(path))
            )
            info.size = file_stat.st_size
            info.mtime = int(file_stat.st_mtime)
            info.mode = stat.S_IMODE(file_stat.st_mode)
            archive.addfile(info, stream)
            return file_stat.st_size

    def _run_bounded_query(self, query: Mapping[str, Any]) -> Any:
        """Enforce a declared query timeout without unbounded helper threads."""

        timeout = query.get("timeout")
        if timeout is None:
            return self.query_runner(query)
        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("artifact query timeout must be positive")
        if not self._query_slots.acquire(False):
            raise RuntimeError(
                "artifact query capacity is exhausted by timed-out vendor calls"
            )
        result = Future()

        def invoke():
            try:
                value = self.query_runner(query)
                if not result.cancelled():
                    result.set_result(value)
            except BaseException as error:
                if not result.cancelled():
                    result.set_exception(error)
            finally:
                self._query_slots.release()

        thread = threading.Thread(
            target=invoke,
            name="dldd-artifact-query",
            daemon=True,
        )
        thread.start()
        try:
            return result.result(timeout=timeout)
        except TimeoutError:
            result.cancel()
            raise RuntimeError(
                "artifact query timed out after {} seconds".format(timeout)
            )

    def status(self, artifact_id: str) -> ArtifactRequest:
        suffix = ".tar.gz"
        base = artifact_id[:-len(suffix)] if artifact_id.endswith(suffix) else ""
        if not _ARTIFACT_BASE_PATTERN.fullmatch(base):
            raise ValueError("invalid DLDD artifact identifier")
        path = os.path.join(self.directory, base + ".json")
        with self._store_lock:
            with open(path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
        return ArtifactRequest(
            artifact_id=str(state["artifact_id"]),
            state=str(state["state"]),
            requested_at=float(state["requested_at"]),
            completed_at=(
                float(state["completed_at"])
                if state.get("completed_at") is not None
                else None
            ),
            last_error=str(state.get("last_error", "")),
        )

    def shutdown(self, wait: bool = True) -> None:
        with self._store_lock:
            should_signal = not self._closed
            self._closed = True
        if should_signal:
            for unused in self._workers:
                if wait:
                    self._jobs.put(None)
                else:
                    try:
                        self._jobs.put_nowait(None)
                    except Full:
                        break
        if wait:
            for worker in self._workers:
                worker.join()

    def _canonical_bases_locked(self):
        result = []
        for name in os.listdir(self.directory):
            if not name.endswith(".json"):
                continue
            base = name[:-5]
            if _ARTIFACT_BASE_PATTERN.fullmatch(base):
                result.append(base)
        return result

    def _entry_count_locked(self) -> int:
        return len(self._canonical_bases_locked())

    def _remove_pair_locked(self, artifact_base: str) -> None:
        for suffix in (".json", ".tar.gz"):
            try:
                os.unlink(os.path.join(self.directory, artifact_base + suffix))
            except FileNotFoundError:
                pass

    def _prune_locked(self, reserve: int = 0) -> None:
        limit = max(0, self.max_artifacts - reserve)
        bases = self._canonical_bases_locked()
        if len(bases) <= limit:
            return
        terminal = sorted(
            (base for base in bases if base not in self._active),
            key=lambda base: os.path.getmtime(
                os.path.join(self.directory, base + ".json")
            ),
        )
        remove_count = len(bases) - limit
        for artifact_base in terminal[:remove_count]:
            self._remove_pair_locked(artifact_base)

    def _valid_archive_locked(self, artifact_base: str) -> bool:
        path = os.path.join(self.directory, artifact_base + ".tar.gz")
        try:
            file_stat = os.lstat(path)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > self.max_artifact_bytes
        ):
            return False
        try:
            with tarfile.open(path, "r:gz") as archive:
                metadata = archive.getmember("metadata.json")
                return metadata.isfile() and metadata.size <= self.max_artifact_bytes
        except (KeyError, OSError, tarfile.TarError):
            return False

    def _load_manifest_locked(self, artifact_base: str):
        path = os.path.join(self.directory, artifact_base + ".json")
        try:
            file_stat = os.lstat(path)
            if not stat.S_ISREG(file_stat.st_mode):
                return None
            with open(path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
            if not isinstance(state, dict):
                return None
            if state.get("artifact_id") != artifact_base + ".tar.gz":
                return None
            if state.get("state") not in (
                "REQUESTED",
                "RUNNING",
                "COMPLETED",
                "FAILED",
            ):
                return None
            return state
        except (OSError, ValueError, TypeError):
            return None

    def _reconcile_store_locked(self) -> None:
        now = time.time()
        names = tuple(os.listdir(self.directory))
        for name in names:
            if _STAGED_ARCHIVE_PATTERN.fullmatch(name):
                path = os.path.join(self.directory, name)
                try:
                    file_stat = os.lstat(path)
                    if stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(
                        file_stat.st_mode
                    ):
                        os.unlink(path)
                except FileNotFoundError:
                    pass

        bases = set()
        for name in os.listdir(self.directory):
            for suffix in (".json", ".tar.gz"):
                if name.endswith(suffix):
                    base = name[: -len(suffix)]
                    if _ARTIFACT_BASE_PATTERN.fullmatch(base):
                        bases.add(base)
                    break

        for artifact_base in bases:
            manifest_path = os.path.join(
                self.directory, artifact_base + ".json"
            )
            archive_path = os.path.join(
                self.directory, artifact_base + ".tar.gz"
            )
            state = self._load_manifest_locked(artifact_base)
            if self._valid_archive_locked(artifact_base):
                requested_at = os.path.getmtime(archive_path)
                if state is not None:
                    try:
                        requested_at = float(state.get("requested_at", requested_at))
                    except (TypeError, ValueError):
                        pass
                _atomic_json(
                    manifest_path,
                    ArtifactRequest(
                        artifact_base + ".tar.gz",
                        "COMPLETED",
                        requested_at,
                        os.path.getmtime(archive_path),
                    ).as_payload(),
                )
                continue

            try:
                os.unlink(archive_path)
            except FileNotFoundError:
                pass
            if state is None:
                try:
                    os.unlink(manifest_path)
                except FileNotFoundError:
                    pass
                continue
            if state.get("state") in ("REQUESTED", "RUNNING", "COMPLETED"):
                try:
                    requested_at = float(state.get("requested_at", now))
                except (TypeError, ValueError):
                    requested_at = now
                _atomic_json(
                    manifest_path,
                    ArtifactRequest(
                        artifact_base + ".tar.gz",
                        "FAILED",
                        requested_at,
                        now,
                        "artifact generation was interrupted before completion",
                    ).as_payload(),
                )

    @staticmethod
    def _run_query(query: Mapping[str, Any]) -> Any:
        resolved_executor = query.get("executor")
        if callable(resolved_executor):
            return resolved_executor(query)
        if query.get("type") != "cli":
            raise RuntimeError("a query runner must be registered for non-CLI queries")
        import subprocess

        result = subprocess.run(
            list(query["argv"]),
            shell=False,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=query.get("timeout"),
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8", "replace")
