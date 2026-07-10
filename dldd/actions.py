"""Bounded, asynchronous vendor action execution."""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from queue import Queue
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

from .bounded_calls import BoundedCallGate
from .command_execution import build_i2c_argv, run_shell_free
from .hooks import VendorHookRegistry
from .models import Operation
from .timestamps import floor_timestamp_fields


@dataclass(frozen=True)
class ActionResult:
    type: str
    status: str
    started_at: float
    completed_at: float
    command: Any = None
    output: Any = None
    error: str = ""

    def as_payload(self) -> Mapping[str, Any]:
        payload = {
            "type": self.type,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.command is not None:
            payload["command"] = self.command
        if self.output is not None:
            payload["output"] = self.output
        if self.error:
            payload["error"] = self.error
        return floor_timestamp_fields(payload)


@dataclass(frozen=True)
class ActionSequenceResult:
    worker_id: str
    state: str
    started_at: float
    completed_at: float
    actions: Tuple[ActionResult, ...]
    last_error: str = ""


class ActionExecutor:
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
            result = run_shell_free(
                list(argv),
                timeout=timeout,
                max_output_bytes=int(
                    action.get("max_output_bytes", 1024 * 1024)
                ),
            )
            if result.returncode:
                raise RuntimeError(
                    "CLI action exited {}: {}".format(
                        result.returncode, result.stderr_text()
                    )
                )
            return result.stdout_text()
        if action_type == "i2c":
            return (
                self.i2c_action(action)
                if self.i2c_action is not None
                else self._execute_i2c(action, timeout)
            )

        hook_name = action.get("hook")
        if not hook_name and action_type == "dse":
            hook_name = "dse"
        if not hook_name:
            hook_name = str(action_type)
        return self.hooks.get(str(hook_name)).execute_action(action)

    def _execute_i2c(self, action: Mapping[str, Any], timeout: float) -> Any:
        path = action.get("path") or {}
        self.hooks.validate_i2c_source(path)
        operation = path.get("i2c_type")
        if operation not in ("get", "set"):
            raise ValueError("I2C action requires get or set")
        configured_buses = path.get("bus")
        buses = (
            list(configured_buses)
            if isinstance(configured_buses, (list, tuple))
            else [configured_buses]
        )
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
        result = run_shell_free(
            build_i2c_argv(path, operation=operation, bus=bus),
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(result.stderr_text())
        return result.stdout_text().strip()


class ActionRunner:
    """Run ordered sequences away from the primary orchestration thread."""

    def __init__(self, executor: ActionExecutor, max_workers: int = 4) -> None:
        self.executor = executor
        max_workers = max(1, int(max_workers))
        self._jobs = Queue()
        self._sequence_slots = threading.BoundedSemaphore(max_workers)
        self._call_gate = BoundedCallGate(max_workers, "dldd-action-call")
        self._closed = False
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                name="dldd-actions-{}".format(index),
                daemon=True,
            )
            for index in range(max_workers)
        )
        for worker in self._workers:
            worker.start()

    def submit(
        self,
        rule_name: str,
        actions: Iterable[Mapping[str, Any]],
        default_timeout: Optional[float],
    ) -> Future:
        worker_id = "action-{}-{}".format(rule_name, uuid.uuid4().hex[:12])
        future = Future()
        future.dldd_worker_id = worker_id
        if self._closed:
            future.set_exception(RuntimeError("action runner is shut down"))
            return future
        if not self._sequence_slots.acquire(False):
            now = time.time()
            future.set_result(
                ActionSequenceResult(
                    worker_id,
                    "FAILED",
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

    def _start_call(self, action: Mapping[str, Any], timeout: float) -> Future:
        return self._call_gate.start(
            lambda: self.executor.execute(action, timeout),
            "action execution capacity is exhausted by timed-out vendor calls",
        )

    def _run_sequence(
        self,
        worker_id: str,
        actions: Tuple[Mapping[str, Any], ...],
        default_timeout: Optional[float],
    ) -> ActionSequenceResult:
        started = time.time()
        results = []
        last_error = ""
        for action in actions:
            action_started = time.time()
            timeout = action.get("timeout", default_timeout)
            if timeout is None:
                last_error = "local action has no timeout"
                results.append(
                    ActionResult(
                        str(action.get("type", "")),
                        "FAILED",
                        action_started,
                        time.time(),
                        action.get("command", action.get("argv")),
                        error=last_error,
                    )
                )
                break
            try:
                call = self._start_call(action, float(timeout))
                output = call.result(timeout=float(timeout))
                results.append(
                    ActionResult(
                        str(action.get("type", "")),
                        "SUCCESS",
                        action_started,
                        time.time(),
                        action.get("command", action.get("argv")),
                        output=output,
                    )
                )
            except TimeoutError:
                call.cancel()
                last_error = "action timed out after {} seconds".format(timeout)
                results.append(
                    ActionResult(
                        str(action.get("type", "")),
                        "FAILED",
                        action_started,
                        time.time(),
                        action.get("command", action.get("argv")),
                        error=last_error,
                    )
                )
                break
            except Exception as error:
                last_error = str(error)
                results.append(
                    ActionResult(
                        str(action.get("type", "")),
                        "FAILED",
                        action_started,
                        time.time(),
                        action.get("command", action.get("argv")),
                        error=last_error,
                    )
                )
                break
        return ActionSequenceResult(
            worker_id,
            "FAILED" if last_error else "COMPLETED",
            started,
            time.time(),
            tuple(results),
            last_error,
        )

    def shutdown(self, wait: bool = True) -> None:
        if not self._closed:
            self._closed = True
            for unused in self._workers:
                self._jobs.put(None)
        if wait:
            for worker in self._workers:
                worker.join()
