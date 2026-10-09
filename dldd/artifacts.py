"""DLDD-specific collection for artifacts packaged by host Healthz."""

from __future__ import annotations

import glob
import json
import logging
import os
import stat
import tempfile
import threading
from concurrent.futures import TimeoutError
from dataclasses import dataclass
from queue import Full, Queue
from typing import Any, Callable, Iterable, Mapping, Optional

from .bounded_calls import BoundedCallGate, start_daemon_workers
from .command_execution import DEFAULT_MAX_OUTPUT_BYTES, run_checked_shell_free
from .models import Operation
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIRECTORY = "/var/lib/sonic/dldd/artifacts"
_HEALTHZ_BUS = "org.SONiC.HostService.healthz"
_HEALTHZ_PATH = "/org/SONiC/HostService/healthz"
_MAX_PATHS = 128
_MAX_REQUEST_BYTES = 64 * 1024


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

    def artifact_status(self, artifact_id: str) -> Optional[str]:
        """Return Healthz availability, or None if this client cannot check."""

        return None

    def shutdown(self, wait: bool = True) -> None:
        """Release worker resources owned by the artifact implementation."""


class HostHealthzArtifactClient(HealthzArtifactClient):
    """Run DLDD queries, then ask the host Healthz service to package files."""

    def __init__(
        self,
        query_runner: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        max_workers: int = 2,
        max_jobs: int = 20,
        max_artifact_bytes: int = 50 * 1024 * 1024,
        healthz_call: Optional[Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] = None,
    ) -> None:
        self.query_runner = query_runner or self._run_query
        self.max_artifact_bytes = max(1024, max_artifact_bytes)
        self._call = healthz_call or self._dbus_call
        self._jobs: Queue = Queue(maxsize=max(1, max_jobs))
        self._query_gate = BoundedCallGate(max_workers, "dldd-artifact-query")
        self._lock = threading.Lock()
        self._closed = False
        self._workers = start_daemon_workers(max_workers, "dldd-artifacts-", self._worker)

    @staticmethod
    def _dbus_call(method: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        # Reserve runs on the DLDD thread; submit runs on a collection worker.
        import dbus

        raw = json.dumps(request, separators=(",", ":"))
        if len(raw.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ValueError("Healthz artifact request is too large")
        endpoint = dbus.SystemBus().get_object(_HEALTHZ_BUS, _HEALTHZ_PATH)
        code, payload = getattr(endpoint, method)(
            raw, dbus_interface=_HEALTHZ_BUS,
            timeout=120 if method == "submit_artifact" else 5,
        )
        if int(code):
            raise RuntimeError("Healthz {} failed: {}".format(method, payload))
        result = json.loads(str(payload))
        if not isinstance(result, dict):
            raise ValueError("Healthz {} returned an invalid response".format(method))
        return result

    def request(self, metadata, logs, queries) -> ArtifactReference:
        job_data = (dict(metadata), tuple(logs), tuple(queries))
        with self._lock:
            if self._closed:
                raise RuntimeError("artifact client is shut down")
            if self._jobs.full():
                raise RuntimeError("artifact collection queue is full")
            reserved = self._call("reserve_artifact", {})
            artifact_id = reserved["artifact_id"]
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError("Healthz returned an invalid artifact ID")
            reference = ArtifactReference(
                artifact_id,
                float(reserved["requested_at"]),
                str(reserved["location"]),
            )
            self._jobs.put_nowait((artifact_id,) + job_data)
        return reference

    def artifact_status(self, artifact_id: str) -> Optional[str]:
        result = self._call("artifact_status", {"artifact_id": artifact_id})
        state = result.get("state")
        if state not in ("PENDING", "COMPLETED", "MISSING"):
            raise ValueError("Healthz returned an invalid artifact state")
        return state

    def fail_artifact(self, artifact_id: str) -> None:
        self._call("fail_artifact", {"artifact_id": artifact_id})

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                artifact_id = job[0]
                try:
                    self._collect(*job)
                except Exception:
                    LOGGER.exception("DLDD artifact collection failed for %s", artifact_id)
                    try:
                        self.fail_artifact(artifact_id)
                    except Exception:
                        LOGGER.exception("unable to release Healthz reservation %s", artifact_id)
            finally:
                self._jobs.task_done()

    def _collect(self, artifact_id, metadata, logs, queries) -> None:
        action_outputs = tuple(metadata.pop("action_outputs", ()))
        metadata = floor_timestamp_fields(metadata)
        paths = []
        size = len(json.dumps(metadata, sort_keys=True).encode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="dldd-healthz-") as stage:
            stage = os.path.realpath(stage)
            def add_data(name, data):
                nonlocal size
                if not data or size + len(data) > self.max_artifact_bytes:
                    return
                path = os.path.join(stage, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as stream:
                    stream.write(data)
                paths.append({"path": path, "name": name})
                size += len(data)

            for index, output in enumerate(action_outputs):
                action_index = int(output.get("index", index))
                prefix = "actions/{:03d}".format(action_index)
                summary = {
                    key: value for key, value in output.items()
                    if key not in ("stdout", "stderr", "result")
                    and value not in (None, "", [], ())
                }
                add_data(prefix + "/metadata.json", json.dumps(
                    summary, sort_keys=True, indent=2
                ).encode())
                for field in ("stdout", "stderr", "result"):
                    add_data(
                        prefix + "/" + field + ".txt",
                        str(output.get(field, "")).encode("utf-8", "replace"),
                    )

            for index, query in enumerate(queries):
                output = self._run_bounded_query(query)
                data = output if isinstance(output, bytes) else str(output).encode(
                    "utf-8", "replace"
                )
                data = data[:int(query.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))]
                add_data("queries/{:03d}.txt".format(index), data)

            for path in self._resolve_logs(logs):
                if len(paths) >= _MAX_PATHS:
                    break
                name = "logs/" + path.lstrip(os.path.sep)
                if len(name) > 512 or "\\" in name:
                    continue
                try:
                    source = os.fdopen(os.open(
                        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    ), "rb")
                except OSError:
                    continue
                with source:
                    info = os.fstat(source.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0
                            or size + info.st_size > self.max_artifact_bytes):
                        continue
                    staged = os.path.join(stage, name)
                    os.makedirs(os.path.dirname(staged), exist_ok=True)
                    remaining = info.st_size
                    with open(staged, "wb") as target:
                        while remaining:
                            data = source.read(min(64 * 1024, remaining))
                            if not data:
                                break
                            target.write(data)
                            remaining -= len(data)
                    if remaining == info.st_size:
                        os.unlink(staged)
                        continue
                    paths.append({"path": staged, "name": name})
                    size += info.st_size - remaining
            if len(paths) > _MAX_PATHS:
                raise RuntimeError("too many DLDD artifact files")
            self._call("submit_artifact", {
                "artifact_id": artifact_id,
                "metadata": metadata,
                "paths": paths,
            })

    @staticmethod
    def _resolve_logs(patterns: Iterable[str]):
        seen = set()
        count = 0
        for pattern in patterns:
            for match in glob.iglob(pattern):
                path = os.path.abspath(match)
                if path in seen:
                    continue
                seen.add(path)
                try:
                    if not stat.S_ISREG(os.lstat(path).st_mode):
                        continue
                except OSError:
                    continue
                yield path
                count += 1
                if count >= _MAX_PATHS:
                    return

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
        except TimeoutError as error:
            result.cancel()
            raise RuntimeError(
                "artifact query timed out after {} seconds".format(timeout)
            ) from error

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            should_signal = not self._closed
            self._closed = True
        if should_signal:
            for _unused in self._workers:
                try:
                    self._jobs.put(None) if wait else self._jobs.put_nowait(None)
                except Full:
                    break
        if wait:
            for worker in self._workers:
                worker.join()

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
