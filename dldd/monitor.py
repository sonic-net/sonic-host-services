"""Monitor threads and their single-flight state machine."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from queue import Empty, Full, Queue
from typing import Dict, Optional

from .adapters import DataSourceAdapter
from .runtime import (
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
    MonitorCommandType,
    MonitorControlCommand,
    MonitorExecutionPlan,
    MonitorWorkState,
    MonitorWorkStateRecord,
    RuleRuntimeStatus,
    SourceAvailability,
)


LOGGER = logging.getLogger(__name__)


class MonitorThread(threading.Thread):
    """Poll one immutable plan and own all writes to its state map."""

    def __init__(
        self,
        plan: MonitorExecutionPlan,
        adapters: Dict[str, DataSourceAdapter],
        evidence_queue: Queue,
        fault_evidence_ack_timeout: float = 120.0,
        source_recovery_samples: int = 1,
        stop_event: Optional[threading.Event] = None,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        super().__init__(name="dldd-{}".format(plan.monitor_id), daemon=True)
        self.plan = plan
        self.adapters = adapters
        self.evidence_queue = evidence_queue
        self.fault_evidence_ack_timeout = fault_evidence_ack_timeout
        self.source_recovery_samples = max(1, source_recovery_samples)
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.wall_clock = wall_clock
        self._sequence = 0
        self._next_poll = self.clock()
        self._schedule_lock = threading.Lock()
        self.diagnostics = deque(maxlen=32)

    def stop(self) -> None:
        self.stop_event.set()

    def update_polling_interval(self, interval: float) -> None:
        """Apply a dynamic interval and pull an overly distant poll forward."""

        interval = max(0.1, float(interval))
        self.plan.polling_interval = interval
        with self._schedule_lock:
            self._next_poll = min(self._next_poll, self.clock() + interval)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("unhandled monitor cycle error in %s", self.plan.monitor_id)
            self.stop_event.wait(0.2)

    def run_once(self) -> None:
        self.drain_control_queue()
        now = self.clock()
        self._recover_expired_ownership(now)
        with self._schedule_lock:
            normal_poll_due = now >= self._next_poll
        recheck_due = any(
            state.state == MonitorWorkState.RECHECK_REQUESTED
            and (
                state.recheck_not_before is None
                or now >= state.recheck_not_before
            )
            for state in self.plan.state_by_key.values()
        )
        if not normal_poll_due and not recheck_due:
            return
        if normal_poll_due:
            with self._schedule_lock:
                self._next_poll = now + max(0.1, self.plan.polling_interval)
        self.poll_once(now, include_normal=normal_poll_due)

    def drain_control_queue(self) -> None:
        while True:
            try:
                command = self.plan.control_queue.get_nowait()
            except Empty:
                return
            try:
                self.apply_command(command)
            finally:
                self.plan.control_queue.task_done()

    def apply_command(self, command: MonitorControlCommand) -> bool:
        if command.monitor_id != self.plan.monitor_id:
            LOGGER.warning("discarding command for monitor %s", command.monitor_id)
            return False
        if command.plan_generation != self.plan.plan_generation:
            LOGGER.warning("discarding stale command generation %s", command.command_id)
            return False
        state = self.plan.state_by_key.get(command.correlation_key)
        if state is None:
            LOGGER.warning("discarding command for unknown key %s", command.correlation_key)
            return False
        if (
            command.expected_work_state_generation is not None
            and command.expected_work_state_generation != state.work_state_generation
        ):
            LOGGER.warning("discarding stale work-state command %s", command.command_id)
            return False

        if command.command == MonitorCommandType.RESUME:
            target = command.target_state
            if target not in (MonitorWorkState.READY, MonitorWorkState.DEGRADED):
                target = MonitorWorkState.READY
            self._transition(state, target)
            state.ack_deadline = None
            state.hold_deadline = None
            state.recheck_not_before = None
        elif command.command == MonitorCommandType.HOLD:
            self._transition(state, MonitorWorkState.HELD_BY_PRIMARY)
            state.ack_deadline = None
            state.hold_deadline = command.hold_deadline
        elif command.command == MonitorCommandType.RECHECK_ONCE:
            self._transition(state, MonitorWorkState.RECHECK_REQUESTED)
            state.ack_deadline = None
            state.hold_deadline = command.hold_deadline
            state.recheck_not_before = command.recheck_not_before
        elif command.command == MonitorCommandType.SUSPEND:
            target = command.target_state
            if target not in (MonitorWorkState.BROKEN, MonitorWorkState.SUSPENDED):
                target = MonitorWorkState.SUSPENDED
            self._transition(state, target)
            state.ack_deadline = None
            state.hold_deadline = None
            state.recheck_not_before = None
        else:
            LOGGER.warning("discarding unsupported command %s", command.command)
            return False
        return True

    @staticmethod
    def _transition(state: MonitorWorkStateRecord, target: MonitorWorkState) -> None:
        state.state = target
        state.work_state_generation += 1

    def _recover_expired_ownership(self, now: float) -> None:
        for key, state in self.plan.state_by_key.items():
            if (
                state.state == MonitorWorkState.IN_FLIGHT
                and state.ack_deadline is not None
                and now >= state.ack_deadline
            ):
                LOGGER.error("primary acknowledgement lease expired for %s", key)
                self._record_lease_expiry(key, "IN_FLIGHT", now)
                self._transition(state, MonitorWorkState.READY)
                state.ack_deadline = None
            elif (
                state.state == MonitorWorkState.HELD_BY_PRIMARY
                and state.hold_deadline is not None
                and now >= state.hold_deadline
            ):
                LOGGER.error("primary hold deadline expired for %s", key)
                self._record_lease_expiry(key, "HELD_BY_PRIMARY", now)
                self._transition(state, MonitorWorkState.READY)
                state.hold_deadline = None
            elif (
                state.state == MonitorWorkState.RECHECK_REQUESTED
                and state.hold_deadline is not None
                and now >= state.hold_deadline
            ):
                LOGGER.error("primary recheck deadline expired for %s", key)
                self._record_lease_expiry(key, "RECHECK_REQUESTED", now)
                self._transition(state, MonitorWorkState.READY)
                state.hold_deadline = None
                state.recheck_not_before = None

    def _record_lease_expiry(self, key: str, state: str, now: float) -> None:
        self.diagnostics.append(
            {
                "monitor": self.plan.monitor_id,
                "correlation_key": key,
                "state": state,
                "reason": "primary ownership lease expired",
                "observed_at": self.wall_clock(),
                "monotonic_at": now,
            }
        )

    def poll_once(
        self, now: Optional[float] = None, include_normal: bool = True
    ) -> None:
        now = self.clock() if now is None else now
        for key in sorted(self.plan.items_by_key):
            state = self.plan.state_by_key[key]
            recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
            if not recheck and (
                not include_normal
                or state.state
                not in (MonitorWorkState.READY, MonitorWorkState.DEGRADED)
            ):
                continue
            if (
                recheck
                and state.recheck_not_before is not None
                and now < state.recheck_not_before
            ):
                continue
            self._collect_key(key, state)

    def _collect_key(self, key: str, state: MonitorWorkStateRecord) -> None:
        item = self.plan.items_by_key[key]
        from_recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
        try:
            adapter = self.adapters[item.source_type]
            result = adapter.collect(item)
        except Exception as error:
            result = EvaluationResult(
                EvaluationResultType.COLLECTION_ERROR,
                completed_at=self.wall_clock(),
                error_category="MONITOR_ERROR",
                error=str(error),
                retryable=True,
            )
        previous_sample = state.last_sample_state
        previous_source = state.source_status

        if result.result in (EvaluationResultType.MATCH, EvaluationResultType.NO_MATCH):
            state.last_success_timestamp = self.wall_clock()
            state.consecutive_failure_count = 0
            if previous_source == SourceAvailability.UNAVAILABLE:
                state.recovery_success_count += 1
                if state.recovery_success_count < self.source_recovery_samples:
                    return
                recovered = EvaluationResult(
                    EvaluationResultType.SOURCE_RECOVERED,
                    value=result.value,
                    evaluator_type=result.evaluator_type,
                    operator=result.operator,
                    expected=result.expected,
                    condition_config=result.condition_config,
                    source_status=SourceAvailability.RECOVERED,
                    collection_started_at=result.collection_started_at,
                    completed_at=result.completed_at,
                )
                if self._enqueue(item, state, recovered, from_recheck):
                    state.source_status = SourceAvailability.AVAILABLE
                    state.recovery_success_count = 0
                return
            state.source_status = SourceAvailability.AVAILABLE
            state.recovery_success_count = 0
            if result.result == EvaluationResultType.MATCH:
                if self._enqueue(item, state, result, from_recheck):
                    state.last_sample_state = EvaluationResultType.MATCH.value
                return
            if previous_sample == EvaluationResultType.MATCH.value or from_recheck:
                if self._enqueue(item, state, result, from_recheck):
                    state.last_sample_state = EvaluationResultType.NO_MATCH.value
            else:
                state.last_sample_state = EvaluationResultType.NO_MATCH.value
            return

        state.consecutive_failure_count += 1
        state.recovery_success_count = 0
        if result.result in (
            EvaluationResultType.SOURCE_UNAVAILABLE,
            EvaluationResultType.COLLECTION_ERROR,
        ):
            state.source_status = SourceAvailability.UNAVAILABLE
        if not self._enqueue(item, state, result, from_recheck):
            state.source_status = previous_source

    def _enqueue(
        self,
        item,
        state: MonitorWorkStateRecord,
        result: EvaluationResult,
        from_recheck: bool,
    ) -> bool:
        self._sequence += 1
        previous_work_state = state.state
        self._transition(state, MonitorWorkState.IN_FLIGHT)
        enqueued_at = self.wall_clock()
        runtime_status = None
        if result.result in (
            EvaluationResultType.SOURCE_UNAVAILABLE,
            EvaluationResultType.SOURCE_RECOVERED,
            EvaluationResultType.COLLECTION_ERROR,
            EvaluationResultType.EVALUATION_ERROR,
        ):
            runtime_status = RuleRuntimeStatus(
                state="BROKEN" if not result.retryable else "DEGRADED",
                rule_id=item.rule_id,
                rule_name=item.rule_name,
                event_id=item.event_id,
                component_name=item.component_name,
                source_id=item.source_id,
                correlation_key=item.correlation_key,
                reason=result.error,
                error_category=result.error_category,
                failure_count=state.consecutive_failure_count,
                last_success_timestamp=state.last_success_timestamp,
                last_attempt_timestamp=enqueued_at,
                retryable=result.retryable,
            )
        event = FaultEvidenceEvent(
            signature_id=item.rule_id,
            event_id=item.event_id,
            component_name=item.component_name,
            source_id=item.source_id,
            correlation_key=item.correlation_key,
            monitor_id=self.plan.monitor_id,
            plan_generation=self.plan.plan_generation,
            work_state_generation=state.work_state_generation,
            sequence=self._sequence,
            event_timestamp=result.completed_at or enqueued_at,
            enqueue_timestamp=enqueued_at,
            result=result,
            from_recheck=from_recheck,
            runtime_status=runtime_status,
        )
        try:
            self.evidence_queue.put_nowait(event)
        except Full:
            # No transition entered the FIFO.  Restore the prior eligibility
            # so a clear, source recovery, or explicit recheck is retried and
            # cannot be silently lost when the bounded queue is saturated.
            LOGGER.error("fault evidence queue is full; releasing %s", item.correlation_key)
            self._transition(state, previous_work_state)
            state.ack_deadline = None
            return False
        state.last_evidence_sequence = self._sequence
        state.last_enqueue_timestamp = enqueued_at
        state.ack_deadline = self.clock() + self.fault_evidence_ack_timeout
        return True


def command_for_event(
    event: FaultEvidenceEvent,
    command: MonitorCommandType,
    target: MonitorWorkState,
    reason: str,
    **kwargs
) -> MonitorControlCommand:
    return MonitorControlCommand(
        command_id=str(uuid.uuid4()),
        monitor_id=event.monitor_id,
        plan_generation=event.plan_generation,
        correlation_key=event.correlation_key,
        command=command,
        target_state=target,
        reason=reason,
        expected_work_state_generation=event.work_state_generation,
        evidence_sequence=event.sequence,
        recheck_not_before=kwargs.get("recheck_not_before"),
        hold_deadline=kwargs.get("hold_deadline"),
    )
