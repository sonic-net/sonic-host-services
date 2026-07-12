"""Monitor threads and their single-flight state machine."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from itertools import count
from queue import Empty, Full, PriorityQueue, Queue
from typing import Callable, Dict, Optional

from .adapters import DataSourceAdapter
from .planner import monitor_type_for_source, work_items_for_dse_expansion
from .runtime import (
    EvaluationResult,
    EvaluationResultType,
    DSEExpansionEvent,
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

DEFAULT_ASYNC_COLLECTION_WORKERS = 8
DEFAULT_ASYNC_COLLECTION_PENDING = 256
DEFAULT_ASYNC_RECHECK_RESERVE = 8


@dataclass(frozen=True)
class AsyncCollectionCompletion:
    token: str
    result: EvaluationResult


class AsyncCollectionPool:
    """Run opted-in single-item collection without blocking monitor threads."""

    def __init__(
        self,
        max_workers: int = DEFAULT_ASYNC_COLLECTION_WORKERS,
        max_pending: int = DEFAULT_ASYNC_COLLECTION_PENDING,
        recheck_reserve: int = DEFAULT_ASYNC_RECHECK_RESERVE,
    ) -> None:
        max_workers = max(1, int(max_workers))
        max_pending = max(0, int(max_pending))
        recheck_reserve = min(max(0, int(recheck_reserve)), max_pending)
        self._jobs = PriorityQueue()
        self._sequence = count()
        self._total_slots = threading.BoundedSemaphore(
            max_workers + max_pending
        )
        self._normal_slots = threading.BoundedSemaphore(
            max_workers + max_pending - recheck_reserve
        )
        self._closed = False
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                name="dldd-async-collection-{}".format(index),
                daemon=True,
            )
            for index in range(max_workers)
        )
        for worker in self._workers:
            worker.start()

    def submit(
        self,
        token: str,
        collector: Callable[[], EvaluationResult],
        completion_queue: Queue,
        high_priority: bool = False,
    ) -> bool:
        normal_slot = not high_priority
        if self._closed:
            return False
        if normal_slot and not self._normal_slots.acquire(False):
            return False
        if not self._total_slots.acquire(False):
            if normal_slot:
                self._normal_slots.release()
            return False
        priority = 0 if high_priority else 1
        self._jobs.put_nowait(
            (
                priority,
                next(self._sequence),
                (token, collector, completion_queue, normal_slot),
            )
        )
        return True

    def _worker(self) -> None:
        while True:
            unused_priority, unused_sequence, job = self._jobs.get()
            try:
                if job is None:
                    return
                token, collector, completion_queue, normal_slot = job
                if self._closed:
                    continue
                try:
                    result = collector()
                except Exception as error:
                    result = EvaluationResult(
                        EvaluationResultType.COLLECTION_ERROR,
                        completed_at=time.time(),
                        error_category="MONITOR_ERROR",
                        error=str(error),
                        retryable=True,
                    )
                completion_queue.put_nowait(
                    AsyncCollectionCompletion(token, result)
                )
            finally:
                if job is not None:
                    self._total_slots.release()
                    if normal_slot:
                        self._normal_slots.release()
                self._jobs.task_done()

    def shutdown(self, wait: bool = True) -> None:
        if not self._closed:
            self._closed = True
            for unused_worker in self._workers:
                self._jobs.put_nowait(
                    (2, next(self._sequence), None)
                )
        if wait:
            for worker in self._workers:
                worker.join()


class MonitorThread(threading.Thread):
    """Poll one immutable plan and own all writes to its state map."""

    def __init__(
        self,
        plan: MonitorExecutionPlan,
        adapters: Dict[str, DataSourceAdapter],
        evidence_queue: Queue,
        fault_evidence_ack_timeout: float = 120.0,
        source_recovery_samples: int = 1,
        async_collection_pool: Optional[AsyncCollectionPool] = None,
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
        self.async_collection_pool = async_collection_pool
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.wall_clock = wall_clock
        self._sequence = 0
        self._next_poll = self.clock()
        self._async_completions = Queue()
        self._async_jobs = {}
        self._dse_templates_by_child = {}
        self.diagnostics = deque(maxlen=32)
        if any(
            item.async_collection
            for item in self.plan.items_by_key.values()
        ) or any(
            template.item.async_collection
            for template in self.plan.templates_by_key.values()
        ):
            if self.async_collection_pool is None:
                raise ValueError(
                    "async collection work requires a shared collection pool"
                )
        for state in self.plan.state_by_key.values():
            if state.state == MonitorWorkState.COLLECTING:
                self._transition(state, MonitorWorkState.READY)
                state.next_sample_due = None

    def stop(self) -> None:
        self.stop_event.set()

    def update_polling_intervals(self, intervals) -> None:
        """Queue all source defaults for atomic application by this thread."""

        self.plan.queue_polling_interval_update(intervals)

    def drain_interval_update_queue(self) -> None:
        """Apply queued defaults while retaining sole ownership of cadence state."""

        while True:
            try:
                intervals = self.plan.interval_update_queue.get_nowait()
            except Empty:
                return
            try:
                self._apply_polling_intervals(intervals)
            finally:
                self.plan.interval_update_queue.task_done()

    def _apply_polling_intervals(self, intervals) -> None:
        intervals = self.plan.validated_polling_intervals(intervals)
        now = self.clock()
        self.plan.polling_intervals = intervals
        self.plan.polling_interval = intervals[self.plan.monitor_type]
        for key, item in self.plan.item_snapshot().items():
            if item.sampling_interval_is_explicit:
                continue
            state = self.plan.state_by_key[key]
            if state.next_sample_due is not None:
                # A shorter default takes effect promptly. A longer default
                # does not postpone work that was already scheduled sooner.
                interval = intervals[monitor_type_for_source(item.source_type)]
                state.next_sample_due = min(state.next_sample_due, now + interval)
        self._refresh_next_poll(now)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("unhandled monitor cycle error in %s", self.plan.monitor_id)
            self.stop_event.wait(0.2)

    def run_once(self) -> None:
        self.drain_async_completions()
        self.drain_interval_update_queue()
        self.drain_control_queue()
        now = self.clock()
        self._recover_expired_ownership(now)
        self._expand_due_templates(now)
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
        self.poll_once(include_normal=normal_poll_due, respect_schedule=True)
        self._refresh_next_poll(self.clock())

    def _refresh_next_poll(self, now: float) -> None:
        due_times = []
        for state in self.plan.state_by_key.values():
            if state.state not in (
                MonitorWorkState.READY,
                MonitorWorkState.DEGRADED,
            ):
                continue
            due_times.append(
                now if state.next_sample_due is None else state.next_sample_due
            )
        for state in self.plan.expansion_state_by_key.values():
            if state.pending_cycle_keys:
                continue
            due_times.append(
                now
                if state.next_expansion_due is None
                else state.next_expansion_due
            )
        self._next_poll = (
            min(due_times)
            if due_times
            else now + max(0.1, self.plan.polling_interval)
        )

    def _make_normal_work_due(self, state: MonitorWorkStateRecord) -> None:
        due = (
            self.clock()
            if state.next_sample_due is None
            else state.next_sample_due
        )
        self._next_poll = min(self._next_poll, due)

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
            self._make_normal_work_due(state)
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
                self._make_normal_work_due(state)
            elif (
                state.state == MonitorWorkState.HELD_BY_PRIMARY
                and state.hold_deadline is not None
                and now >= state.hold_deadline
            ):
                LOGGER.error("primary hold deadline expired for %s", key)
                self._record_lease_expiry(key, "HELD_BY_PRIMARY", now)
                self._transition(state, MonitorWorkState.READY)
                state.hold_deadline = None
                self._make_normal_work_due(state)
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
                self._make_normal_work_due(state)

    def _expand_due_templates(self, now: float) -> None:
        adapter = self.adapters.get("dse")
        if adapter is None:
            return
        for template_id in sorted(self.plan.templates_by_key):
            state = self.plan.expansion_state_by_key[template_id]
            if state.pending_cycle_keys:
                continue
            if (
                state.next_expansion_due is not None
                and now < state.next_expansion_due
            ):
                continue
            self._expand_template(
                template_id,
                self.plan.templates_by_key[template_id],
                state,
                adapter,
                now,
            )

    def _warmup_keys(self, keys):
        return {
            key
            for key in keys
            if self.plan.state_by_key[key].state
            not in (MonitorWorkState.BROKEN, MonitorWorkState.SUSPENDED)
        }

    def _expand_template(
        self, template_id, template, state, adapter, now
    ) -> None:
        policy = template.source_handle.policy
        previous_phase = state.phase
        try:
            result = adapter.expand(template)
            items = work_items_for_dse_expansion(template, result)
        except Exception as error:
            state.last_error = str(error)
            state.next_expansion_due = now + policy.bootstrap_interval
            if state.phase == "STABLE":
                state.phase = "WARMUP"
                state.warmup_cycles_completed = 0
            self.diagnostics.append(
                {
                    "monitor": self.plan.monitor_id,
                    "template_id": template_id,
                    "state": state.phase,
                    "reason": "DSE expansion failed: {}".format(error),
                    "observed_at": self.wall_clock(),
                }
            )
            return

        fingerprint = tuple(
            sorted(
                (item.dse_binding.instance, item.dse_binding.source_id)
                for item in items
                if item.dse_binding is not None
            )
        )
        changed = bool(state.binding_fingerprint) and (
            fingerprint != state.binding_fingerprint
        )
        desired = {item.correlation_key: item for item in items}
        added = []
        for key, item in desired.items():
            existing = self.plan.expanded_items_by_key.get(key)
            if existing == item:
                continue
            added.append(item)

        relinquished = []
        removed = []
        if result.authoritative:
            for key in tuple(state.child_keys - set(desired)):
                child_state = self.plan.state_by_key.get(key)
                if child_state is None or child_state.state not in (
                    MonitorWorkState.READY,
                    MonitorWorkState.DEGRADED,
                    MonitorWorkState.SUSPENDED,
                    MonitorWorkState.BROKEN,
                ):
                    continue
                relinquished.append(key)
                owners = self._dse_templates_by_child.get(key, set())
                if not (owners - {template_id}):
                    removed.append(key)

        # An authoritative result is also primary-thread evidence when its
        # inventory is unchanged or empty.  Persisted DSE faults have no
        # monitor child after restart, so the primary needs the complete
        # instance snapshot to decide whether an old instance is truly gone.
        if added or removed or result.authoritative:
            event = DSEExpansionEvent(
                monitor_id=self.plan.monitor_id,
                plan_generation=self.plan.plan_generation,
                template_id=template_id,
                signature=template.signature,
                added_items=tuple(added),
                removed_keys=tuple(removed),
                present_instances=tuple(
                    sorted({binding.instance for binding in result.bindings})
                ),
                phase=state.phase,
                authoritative=result.authoritative,
                observed_at=self.wall_clock(),
            )
            try:
                # Registration is queued before children can be sampled. FIFO
                # ordering then guarantees the primary correlation table sees
                # the expansion before any evidence from those children.
                self.evidence_queue.put_nowait(event)
            except Full:
                state.last_error = "primary evidence queue is full"
                state.next_expansion_due = now + policy.bootstrap_interval
                self.diagnostics.append(
                    {
                        "monitor": self.plan.monitor_id,
                        "template_id": template_id,
                        "state": state.phase,
                        "reason": "DSE expansion registration queue is full",
                        "observed_at": self.wall_clock(),
                    }
                )
                return

        for key, item in desired.items():
            self.plan.add_expanded_item(item)
            self._dse_templates_by_child.setdefault(key, set()).add(
                template_id
            )
        for key in relinquished:
            owners = self._dse_templates_by_child.get(key, set())
            owners.discard(template_id)
            if owners:
                continue
            self._dse_templates_by_child.pop(key, None)
            self.plan.remove_expanded_item(key)

        retained = state.child_keys - set(relinquished)
        state.child_keys = retained | set(desired)
        state.binding_fingerprint = fingerprint
        state.last_expansion_timestamp = self.wall_clock()
        state.last_error = ""
        state.authoritative = result.authoritative
        state.cycle_id += 1

        if state.phase == "BOOTSTRAP":
            state.bootstrap_scans_completed += 1
            if state.bootstrap_scans_completed >= policy.bootstrap_scans:
                state.phase = "WARMUP"
                state.warmup_cycles_completed = 0
                state.pending_cycle_keys = self._warmup_keys(state.child_keys)
                state.next_expansion_due = None
            else:
                state.next_expansion_due = now + policy.bootstrap_interval
        elif state.phase == "WARMUP":
            if changed:
                state.warmup_cycles_completed = 0
            else:
                state.warmup_cycles_completed += 1
            if state.warmup_cycles_completed >= policy.warmup_cycles:
                state.phase = "STABLE"
                state.pending_cycle_keys.clear()
                state.next_expansion_due = now + policy.stable_interval
            else:
                state.pending_cycle_keys = self._warmup_keys(state.child_keys)
                state.next_expansion_due = (
                    None
                    if state.pending_cycle_keys
                    else now + policy.bootstrap_interval
                )
        else:
            if changed:
                state.phase = "WARMUP"
                state.warmup_cycles_completed = 0
                state.pending_cycle_keys = self._warmup_keys(state.child_keys)
                state.next_expansion_due = None
            else:
                state.next_expansion_due = now + policy.stable_interval

        if state.phase != previous_phase:
            LOGGER.info(
                "DSE template %s discovery phase changed from %s to %s",
                template_id,
                previous_phase,
                state.phase,
            )

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
        self,
        now: Optional[float] = None,
        include_normal: bool = True,
        respect_schedule: bool = False,
    ) -> None:
        self.drain_async_completions()
        cycle_now = self.clock() if now is None else now
        items = self.plan.item_snapshot()
        for key in sorted(items):
            state = self.plan.state_by_key[key]
            recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
            key_now = self.clock() if respect_schedule else cycle_now
            if not recheck and (
                not include_normal
                or state.state
                not in (MonitorWorkState.READY, MonitorWorkState.DEGRADED)
            ):
                continue
            if (
                recheck
                and state.recheck_not_before is not None
                and key_now < state.recheck_not_before
            ):
                continue
            item = items[key]
            if not recheck:
                attempt_time = key_now
                if (
                    respect_schedule
                    and state.next_sample_due is not None
                    and attempt_time < state.next_sample_due
                ):
                    continue
                # Schedule from this key's attempt, not from the cycle start or
                # prior deadline.  This coalesces missed intervals without
                # shortening later keys when an earlier adapter is slow.
                interval = (
                    item.sampling_interval
                    if item.sampling_interval_is_explicit
                    else self.plan.polling_intervals[
                        monitor_type_for_source(item.source_type)
                    ]
                )
            if item.async_collection:
                if self._submit_async_collection(key, state, item):
                    if not recheck:
                        state.next_sample_due = attempt_time + interval
                continue
            if not recheck:
                state.next_sample_due = attempt_time + interval
            self._collect_key(key, state, item=item)
        self.drain_async_completions()

    def _submit_async_collection(self, key, state, item) -> bool:
        from_recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
        previous_state = state.state
        adapter = self.adapters[item.source_type]
        token = uuid.uuid4().hex
        submitted = self.async_collection_pool.submit(
            token,
            lambda: self._collect_result(adapter, item),
            self._async_completions,
            high_priority=from_recheck,
        )
        if not submitted:
            self.diagnostics.append(
                {
                    "monitor": self.plan.monitor_id,
                    "correlation_key": key,
                    "state": previous_state.value,
                    "reason": "async collection capacity is exhausted",
                    "observed_at": self.wall_clock(),
                }
            )
            return False
        state.last_attempt_timestamp = self.wall_clock()
        self._transition(state, MonitorWorkState.COLLECTING)
        self._async_jobs[token] = (
            key,
            previous_state,
            from_recheck,
            state.work_state_generation,
        )
        return True

    def drain_async_completions(self) -> None:
        while True:
            try:
                completion = self._async_completions.get_nowait()
            except Empty:
                return
            try:
                pending = self._async_jobs.pop(completion.token, None)
                if pending is None:
                    continue
                key, previous_state, from_recheck, generation = pending
                state = self.plan.state_by_key.get(key)
                if (
                    state is None
                    or state.state != MonitorWorkState.COLLECTING
                    or state.work_state_generation != generation
                ):
                    self.diagnostics.append(
                        {
                            "monitor": self.plan.monitor_id,
                            "correlation_key": key,
                            "state": "COLLECTING",
                            "reason": "discarded stale async collection result",
                            "observed_at": self.wall_clock(),
                        }
                    )
                    continue
                self._transition(state, previous_state)
                self._handle_result(
                    key,
                    state,
                    self.plan.item(key),
                    completion.result,
                    from_recheck,
                )
                self._mark_dse_cycle_attempt(key)
            finally:
                self._async_completions.task_done()

    def _collect_result(self, adapter, item) -> EvaluationResult:
        try:
            return adapter.collect(item)
        except Exception as error:
            return EvaluationResult(
                EvaluationResultType.COLLECTION_ERROR,
                completed_at=self.wall_clock(),
                error_category="MONITOR_ERROR",
                error=str(error),
                retryable=True,
            )

    def _collect_key(self, key: str, state: MonitorWorkStateRecord, item=None) -> None:
        item = item or self.plan.item(key)
        from_recheck = state.state == MonitorWorkState.RECHECK_REQUESTED
        state.last_attempt_timestamp = self.wall_clock()
        adapter = self.adapters[item.source_type]
        result = self._collect_result(adapter, item)
        self._handle_result(key, state, item, result, from_recheck)
        self._mark_dse_cycle_attempt(key)

    def _mark_dse_cycle_attempt(self, key: str) -> None:
        template_ids = self._dse_templates_by_child.get(key, ())
        if not template_ids:
            return
        for template_id in tuple(template_ids):
            state = self.plan.expansion_state_by_key[template_id]
            if state.phase != "WARMUP":
                continue
            state.pending_cycle_keys.discard(key)
            if not state.pending_cycle_keys:
                state.last_complete_cycle_timestamp = self.wall_clock()
                state.next_expansion_due = self.clock()
                self._next_poll = min(
                    self._next_poll, state.next_expansion_due
                )

    def _handle_result(
        self,
        key: str,
        state: MonitorWorkStateRecord,
        item,
        result: EvaluationResult,
        from_recheck: bool,
    ) -> None:
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
            if not from_recheck:
                state.next_sample_due = self.clock()
                self._next_poll = min(
                    self._next_poll, state.next_sample_due
                )
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
    return _new_monitor_command(
        monitor_id=event.monitor_id,
        plan_generation=event.plan_generation,
        correlation_key=event.correlation_key,
        command=command,
        target=target,
        reason=reason,
        expected_work_state_generation=event.work_state_generation,
        evidence_sequence=event.sequence,
        **kwargs
    )


def command_for_plan(
    plan: MonitorExecutionPlan,
    correlation_key: str,
    command: MonitorCommandType,
    target: MonitorWorkState,
    reason: str,
    evidence: Optional[FaultEvidenceEvent] = None,
    **kwargs
) -> MonitorControlCommand:
    return _new_monitor_command(
        monitor_id=plan.monitor_id,
        plan_generation=plan.plan_generation,
        correlation_key=correlation_key,
        command=command,
        target=target,
        reason=reason,
        expected_work_state_generation=(
            evidence.work_state_generation if evidence is not None else None
        ),
        evidence_sequence=evidence.sequence if evidence is not None else None,
        **kwargs
    )


def _new_monitor_command(
    *,
    monitor_id,
    plan_generation,
    correlation_key,
    command,
    target,
    reason,
    expected_work_state_generation,
    evidence_sequence,
    **kwargs
) -> MonitorControlCommand:
    return MonitorControlCommand(
        command_id=str(uuid.uuid4()),
        monitor_id=monitor_id,
        plan_generation=plan_generation,
        correlation_key=correlation_key,
        command=command,
        target_state=target,
        reason=reason,
        expected_work_state_generation=expected_work_state_generation,
        evidence_sequence=evidence_sequence,
        recheck_not_before=kwargs.get("recheck_not_before"),
        hold_deadline=kwargs.get("hold_deadline"),
    )
