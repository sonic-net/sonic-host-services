"""Bounded, asynchronous vendor action execution."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from functools import partial
from queue import Queue
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

from .bounded_calls import BoundedCallGate, start_daemon_workers
from .command_execution import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ShellFreeResult,
    build_i2c_argv,
    run_checked_shell_free,
    run_shell_free,
)
from .hooks import VendorHookRegistry, operation_hook_name
from .models import Operation
from .timestamps import floor_timestamp_fields


@dataclass(frozen=True)
class ActionOutput:
    """Optional bounded diagnostic content returned by a vendor action."""

    stdout: Any = None
    stderr: Any = None
    result: Any = None


def _output_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    try:
        return json.dumps(value, sort_keys=True, default=str).encode(
            "utf-8", "replace"
        )
    except Exception:
        return "<unrenderable {}>".format(type(value).__name__).encode()


def _capture_action_output(
    value: Any, limit: int
) -> Tuple[Tuple[str, str, str], Tuple[str, ...]]:
    shell_result = value if isinstance(value, ShellFreeResult) else None
    if shell_result is not None:
        output = ActionOutput(shell_result.stdout, shell_result.stderr)
    elif isinstance(value, ActionOutput):
        output = value
    else:
        output = ActionOutput(result=value)

    captured = []
    truncated = []
    for name in ("stdout", "stderr", "result"):
        data = _output_bytes(getattr(output, name))
        captured.append(data[:limit].decode("utf-8", "replace"))
        if len(data) > limit:
            truncated.append(name)
    if shell_result is not None:
        if shell_result.stdout_truncated and "stdout" not in truncated:
            truncated.append("stdout")
        if shell_result.stderr_truncated and "stderr" not in truncated:
            truncated.append("stderr")
    return (captured[0], captured[1], captured[2]), tuple(truncated)


@dataclass(frozen=True)
class ActionResult:
    type: str
    status: str
    started_at: float
    completed_at: float
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    result: str = ""
    returncode: Optional[int] = None
    truncated: Tuple[str, ...] = ()

    def as_payload(self) -> Mapping[str, Any]:
        payload = {
            "type": self.type,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.error:
            payload["error"] = self.error
        return floor_timestamp_fields(payload)

    def as_artifact_payload(self, index: int) -> Optional[Mapping[str, Any]]:
        if not any((self.stdout, self.stderr, self.result, self.truncated)) and (
            self.returncode is None
        ):
            return None
        return floor_timestamp_fields(
            {
                "index": index,
                "type": self.type,
                "status": self.status,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "error": self.error,
                "returncode": self.returncode,
                "truncated": list(self.truncated),
                "stdout": self.stdout,
                "stderr": self.stderr,
                "result": self.result,
            }
        )


@dataclass(frozen=True)
class ActionSequenceResult:
    worker_id: str
    state: str
    started_at: float
    completed_at: float
    actions: Tuple[ActionResult, ...]
    last_error: str = ""


class ActionExecutor:
    """Execute trusted materialized actions through their typed backend."""

    def __init__(
        self,
        hooks: Optional[VendorHookRegistry] = None,
        i2c_action: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    ) -> None:
        self.hooks = hooks or VendorHookRegistry()
        self.i2c_action = i2c_action

    def execute(self, action: Mapping[str, Any], timeout: float) -> Any:
        resolved_executor = action.get("executor")
        if callable(resolved_executor):
            operation = action.get("materialized_operation")
            if not isinstance(operation, Operation):
                raise ValueError(
                    "resolved action executor requires its materialized operation"
                )
            return resolved_executor(operation)
        action_type = action.get("type")
        if action_type == "cli":
            argv = action.get("argv")
            if not isinstance(argv, (list, tuple)) or not argv:
                raise ValueError("CLI action requires argv")
            return run_shell_free(
                list(argv),
                timeout=timeout,
                max_output_bytes=int(
                    action.get(
                        "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES
                    )
                ),
            )
        if action_type == "i2c":
            return (
                self.i2c_action(action)
                if self.i2c_action is not None
                else self._execute_i2c(action, timeout)
            )

        return self.hooks.get(
            operation_hook_name(action)
        ).execute_action(action)

    def _execute_i2c(self, action: Mapping[str, Any], timeout: float) -> Any:
        path = action.get("path") or {}
        self.hooks.validate_i2c_source(path)
        operation = path.get("i2c_type")
        if operation not in ("get", "set"):
            raise ValueError("I2C action requires get or set")
        configured = path.get("bus")
        buses = configured if isinstance(configured, (list, tuple)) else (configured,)
        deadline = time.monotonic() + timeout
        outputs = []
        for bus in buses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("i2c{}".format(operation), timeout)
            outputs.append(
                ActionExecutor._execute_i2c_bus(
                    path,
                    operation,
                    self.hooks.resolve_i2c_bus(bus, path),
                    remaining,
                )
            )
        return outputs[0] if len(outputs) == 1 else outputs

    @staticmethod
    def _execute_i2c_bus(
        path: Mapping[str, Any], operation: str, bus: Any, timeout: float
    ) -> str:
        return run_checked_shell_free(
            build_i2c_argv(path, operation=operation, bus=bus),
            timeout=timeout,
        ).strip()


class ActionRunner:
    """Run ordered sequences away from the primary orchestration thread."""

    def __init__(self, executor: ActionExecutor, max_workers: int = 4) -> None:
        self.executor = executor
        max_workers = max(1, int(max_workers))
        self._jobs: Queue = Queue()
        self._sequence_slots = threading.BoundedSemaphore(max_workers)
        self._call_gate = BoundedCallGate(max_workers, "dldd-action-call")
        self._closed = False
        self._workers = start_daemon_workers(
            max_workers, "dldd-actions-", self._worker
        )

    def submit(
        self,
        rule_name: str,
        actions: Iterable[Mapping[str, Any]],
        default_timeout: Optional[float],
    ) -> Future:
        worker_id = "action-{}-{}".format(rule_name, uuid.uuid4().hex[:12])
        future: Future = Future()
        future.dldd_worker_id = worker_id  # type: ignore[attr-defined]
        if self._closed:
            future.set_exception(RuntimeError("action runner is shut down"))
            return future
        if not self._sequence_slots.acquire(False):
            now = time.time()
            future.set_result(
                ActionSequenceResult(
                    worker_id,
                    "EXECUTION_ERROR",
                    now,
                    now,
                    (),
                    "action sequence capacity is exhausted",
                )
            )
            return future
        self._jobs.put((future, worker_id, tuple(actions), default_timeout))
        return future

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                future, worker_id, actions, default_timeout = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(
                        self._run_sequence(
                            worker_id, actions, default_timeout
                        )
                    )
                except BaseException as error:
                    future.set_exception(
                        RuntimeError(
                            "action worker terminated: {}".format(error)
                        )
                    )
            finally:
                if job is not None:
                    self._sequence_slots.release()
                self._jobs.task_done()

    def _run_sequence(
        self,
        worker_id: str,
        actions: Tuple[Mapping[str, Any], ...],
        default_timeout: Optional[float],
    ) -> ActionSequenceResult:
        started = time.time()
        results = []
        last_error = ""
        sequence_state = "COMPLETED"
        for action in actions:
            action_started = time.time()
            timeout = action.get("timeout", default_timeout)
            stdout = stderr = output_result = ""
            returncode = None
            truncated: Tuple[str, ...] = ()
            call = None
            if timeout is None:
                last_error = "local action has no timeout"
                sequence_state = "EXECUTION_ERROR"
                stderr = last_error
            else:
                try:
                    call_timeout = float(timeout)
                    call = self._call_gate.start(
                        partial(self.executor.execute, action, call_timeout),
                        "action execution capacity is exhausted by timed-out "
                        "vendor calls",
                    )
                    value = call.result(timeout=call_timeout)
                    limit = int(
                        action.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
                    )
                    (stdout, stderr, output_result), truncated = (
                        _capture_action_output(value, limit)
                    )
                    if isinstance(value, ShellFreeResult):
                        returncode = value.returncode
                        if returncode:
                            last_error = "CLI action exited {}: {}".format(
                                returncode, stderr
                            )
                            sequence_state = "EXECUTION_ERROR"
                except (TimeoutError, subprocess.TimeoutExpired):
                    if call is not None:
                        call.cancel()
                    last_error = "action timed out after {} seconds".format(
                        timeout
                    )
                    sequence_state = "TIMED_OUT"
                    stderr = last_error
                except Exception as error:
                    last_error = str(error)
                    sequence_state = "EXECUTION_ERROR"
                    stderr = last_error
            results.append(
                ActionResult(
                    str(action.get("type", "")),
                    sequence_state if last_error else "COMPLETED",
                    action_started,
                    time.time(),
                    error=last_error,
                    stdout=stdout,
                    stderr=stderr,
                    result=output_result,
                    returncode=returncode,
                    truncated=truncated,
                )
            )
            if last_error:
                break
        return ActionSequenceResult(
            worker_id,
            sequence_state,
            started,
            time.time(),
            tuple(results),
            last_error,
        )

    def shutdown(self, wait: bool = True) -> None:
        if not self._closed:
            self._closed = True
            for _unused in self._workers:
                self._jobs.put(None)
        if wait:
            for worker in self._workers:
                worker.join()
