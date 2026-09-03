"""Asynchronous host-side Healthz artifact generation."""

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
from concurrent.futures import TimeoutError
from dataclasses import dataclass
from queue import Full, Queue
from typing import Any, Callable, Iterable, Mapping, Optional

from .bounded_calls import BoundedCallGate, start_daemon_workers
from .command_execution import DEFAULT_MAX_OUTPUT_BYTES, run_checked_shell_free
from .filesystem import unlink_if_exists
from .models import Operation
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIRECTORY = "/var/lib/sonic/dldd/artifacts"
_ARTIFACT_PATTERN = re.compile(r"^dldd-[0-9a-f]{32}\.tar\.gz$")
_STAGED_PATTERN = re.compile(
    r"^\.dldd-[0-9a-f]{32}-[A-Za-z0-9_-]+\.tar\.gz$"
)


@dataclass(frozen=True)
class ArtifactReference:
    """Stable reference published once when collection is accepted."""

    artifact_id: str
    requested_at: float
    location: str

    def as_payload(self) -> Mapping[str, Any]:
        return floor_timestamp_fields(
            {
                "artifact_id": self.artifact_id,
                "requested_at": self.requested_at,
                "location": self.location,
            }
        )


class HealthzArtifactClient:
    def request(
        self,
        metadata: Mapping[str, Any],
        logs: Iterable[str],
        queries: Iterable[Mapping[str, Any]],
    ) -> ArtifactReference:
        """Accept one collection request and return its stable reference."""

        raise NotImplementedError

    def shutdown(self, wait: bool = True) -> None:
        """Release worker resources owned by the artifact implementation."""


class FilesystemArtifactClient(HealthzArtifactClient):
    """Generate final archives atomically in the host Healthz directory."""

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
        self._query_gate = BoundedCallGate(max_workers, "dldd-artifact-query")
        self._store_lock = threading.RLock()
        self._active = set()
        self._closed = False
        os.makedirs(self.directory, mode=0o750, exist_ok=True)
        with self._store_lock:
            self._remove_interrupted_staging_locked()
            self._prune_locked()
        self._workers = start_daemon_workers(
            max_workers, "dldd-artifacts-", self._worker
        )

    def request(self, metadata, logs, queries) -> ArtifactReference:
        requested_at = time.time()
        artifact_id = "dldd-{}.tar.gz".format(uuid.uuid4().hex)
        job = (artifact_id, dict(metadata), tuple(logs), tuple(queries))
        with self._store_lock:
            if self._closed:
                raise RuntimeError("artifact client is shut down")
            self._prune_locked(reserve=1)
            if len(self._archives_locked()) + len(self._active) >= self.max_artifacts:
                raise RuntimeError("artifact store capacity is exhausted")
            self._active.add(artifact_id)
            try:
                self._jobs.put_nowait(job)
            except Exception:
                self._active.discard(artifact_id)
                raise
        return ArtifactReference(
            artifact_id,
            requested_at,
            os.path.join(self.directory, artifact_id),
        )

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                try:
                    self._collect(*job)
                except Exception:
                    LOGGER.exception("artifact generation failed for %s", job[0])
            finally:
                if job is not None:
                    with self._store_lock:
                        self._active.discard(job[0])
                        self._prune_locked()
                self._jobs.task_done()

    def _collect(self, artifact_id, metadata, logs, queries) -> None:
        archive_path = os.path.join(self.directory, artifact_id)
        descriptor, staged = tempfile.mkstemp(
            prefix=".{}-".format(artifact_id[:-7]),
            suffix=".tar.gz",
            dir=self.directory,
        )
        os.close(descriptor)
        try:
            with tarfile.open(staged, "w:gz") as archive:
                bytes_added = self._add_bytes(
                    archive,
                    "metadata.json",
                    json.dumps(
                        floor_timestamp_fields(metadata),
                        sort_keys=True,
                        indent=2,
                    ).encode(),
                    0,
                )
                for pattern in logs:
                    for path in sorted(glob.glob(pattern)):
                        bytes_added += self._add_log_file(
                            archive,
                            path,
                            self.max_artifact_bytes - bytes_added,
                        )
                for index, query in enumerate(queries):
                    output = self._run_bounded_query(query)
                    data = output if isinstance(output, bytes) else str(output).encode(
                        "utf-8", "replace"
                    )
                    data = data[: int(query.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))]
                    if bytes_added + len(data) > self.max_artifact_bytes:
                        break
                    bytes_added = self._add_bytes(
                        archive,
                        "queries/{:03d}.txt".format(index),
                        data,
                        bytes_added,
                    )
            if os.path.getsize(staged) > self.max_artifact_bytes:
                raise RuntimeError("generated artifact exceeds the size limit")
            with open(staged, "rb") as stream:
                os.fsync(stream.fileno())
            os.replace(staged, archive_path)
        finally:
            unlink_if_exists(staged)

    def _add_bytes(self, archive, name, data, bytes_added):
        if bytes_added + len(data) > self.max_artifact_bytes:
            raise RuntimeError("artifact content exceeds the size limit")
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(data))
        return bytes_added + len(data)

    @staticmethod
    def _add_log_file(archive, path: str, remaining: int) -> int:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return 0
        try:
            stream = os.fdopen(descriptor, "rb")
        except OSError:
            os.close(descriptor)
            return 0
        with stream:
            file_stat = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > remaining
            ):
                return 0
            info = tarfile.TarInfo(os.path.join("logs", os.path.basename(path)))
            info.size = file_stat.st_size
            info.mtime = int(file_stat.st_mtime)
            info.mode = stat.S_IMODE(file_stat.st_mode)
            archive.addfile(info, stream)
            return file_stat.st_size

    def _run_bounded_query(self, query: Mapping[str, Any]) -> Any:
        timeout = query.get("timeout")
        if timeout is None:
            return self.query_runner(query)
        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("artifact query timeout must be positive")
        result = self._query_gate.start(
            lambda: self.query_runner(query),
            "artifact query capacity is exhausted by timed-out vendor calls",
        )
        try:
            return result.result(timeout=timeout)
        except TimeoutError:
            result.cancel()
            raise RuntimeError(
                "artifact query timed out after {} seconds".format(timeout)
            )

    def shutdown(self, wait: bool = True) -> None:
        with self._store_lock:
            should_signal = not self._closed
            self._closed = True
        if should_signal:
            for unused in self._workers:
                try:
                    self._jobs.put(None) if wait else self._jobs.put_nowait(None)
                except Full:
                    break
        if wait:
            for worker in self._workers:
                worker.join()

    def _archives_locked(self):
        return [
            name
            for name in os.listdir(self.directory)
            if _ARTIFACT_PATTERN.fullmatch(name)
        ]

    def _prune_locked(self, reserve: int = 0) -> None:
        limit = max(0, self.max_artifacts - reserve - len(self._active))
        archives = sorted(
            self._archives_locked(),
            key=lambda name: os.path.getmtime(os.path.join(self.directory, name)),
        )
        for name in archives[: max(0, len(archives) - limit)]:
            unlink_if_exists(os.path.join(self.directory, name))

    def _remove_interrupted_staging_locked(self) -> None:
        for name in os.listdir(self.directory):
            if _STAGED_PATTERN.fullmatch(name):
                unlink_if_exists(os.path.join(self.directory, name))

    @staticmethod
    def _run_query(query: Mapping[str, Any]) -> Any:
        resolved_executor = query.get("executor")
        if callable(resolved_executor):
            operation = query.get("materialized_operation")
            if not isinstance(operation, Operation):
                raise ValueError(
                    "resolved query executor requires its materialized operation"
                )
            return resolved_executor(operation)
        if query.get("type") != "cli":
            raise RuntimeError("a query runner must be registered for non-CLI queries")
        return run_checked_shell_free(
            list(query["argv"]),
            timeout=query.get("timeout"),
            max_output_bytes=int(query.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)),
        )
