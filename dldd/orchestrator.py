"""Primary-thread evidence processing and fault lifecycle coordination."""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Dict, Mapping, Optional, Set, Tuple

from .actions import ActionRunner, ActionSequenceResult
from .artifacts import HealthzArtifactClient
from .config import DLDDConfig
from .correlation import CorrelationDecision, CorrelationEngine, FaultArbiter, SignatureExecution
from .runtime import (
    EvaluationResultType,
    FaultEvidenceEvent,
    FaultRecord,
    MonitorCommandType,
    MonitorControlCommand,
    MonitorExecutionPlan,
    MonitorWorkState,
)
from .telemetry import TelemetryPublisher


LOGGER = logging.getLogger(__name__)


@dataclass
class PendingFault:
    execution: SignatureExecution
    first_decision: CorrelationDecision
    future: Any
    hold_deadline: float
    action_deadline: float
    phase: str = "ACTIONS"
    action_result: Optional[ActionSequenceResult] = None
    wait_until: Optional[float] = None
    outstanding_rechecks: Set[str] = field(default_factory=set)
    last_decision: Optional[CorrelationDecision] = None
    artifact: Optional[Mapping[str, Any]] = None
    source_failed: bool = False
    recheck_failed: bool = False
    recheck_deadline: Optional[float] = None
    recheck_attempts: int = 0


@dataclass
class Reconciliation:
    execution: SignatureExecution
    record: FaultRecord
    outstanding_rechecks: Set[str]
    last_decision: Optional[CorrelationDecision] = None
    source_failed: bool = False
    recheck_failed: bool = False
    reason: str = "bootstrap_fault_reconciliation"
    hold_deadline: float = 0.0
    recheck_deadline: float = 0.0
    recheck_attempts: int = 1


class PrimaryOrchestrator:
    def __init__(
        self,
        evidence_queue: Queue,
        plans: Mapping[str, MonitorExecutionPlan],
        work_items: Mapping[str, Any],
        correlation: CorrelationEngine,
        telemetry: TelemetryPublisher,
        config: DLDDConfig,
        active_rules_checksum: str,
        action_runner: Optional[ActionRunner] = None,
        artifact_client: Optional[HealthzArtifactClient] = None,
        local_action_default_timeout: Optional[int] = None,
        source_lifecycle_probe=None,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        self.evidence_queue = evidence_queue
        self.plans = dict(plans)
        self.work_items = dict(work_items)
        self.correlation = correlation
        self.telemetry = telemetry
        self.config = config
        self.active_rules_checksum = active_rules_checksum
        self.action_runner = action_runner
        self.artifact_client = artifact_client
        self.local_action_default_timeout = local_action_default_timeout
        self.source_lifecycle_probe = source_lifecycle_probe
        self.clock = clock
        self.wall_clock = wall_clock
        self.arbiter = FaultArbiter()
        self.pending: Dict[Tuple[int, str], PendingFault] = {}
        self.faults: Dict[Tuple[int, str], FaultRecord] = {}
        self.published_by_key: Dict[Tuple[str, str], int] = {}
        self.broken_rules: Dict[str, Mapping[str, Any]] = {}
        self.source_status: Dict[str, Mapping[str, Any]] = {}
        self._source_unavailable_since: Dict[str, float] = {}
        self._source_failure_keys: Dict[str, Set[str]] = {}
        self._suspended_sources: Dict[str, Set[str]] = {}
        self._next_source_lifecycle_probe = 0.0
        self.reconciliation: Dict[Tuple[int, str], Reconciliation] = {}
        self.next_active_recheck: Dict[Tuple[int, str], float] = {}
        self.uncertain_faults: Set[Tuple[int, str]] = set()
        self.dirty_faults: Set[Tuple[int, str]] = set()
        self._primary_processing_failures: Dict[str, int] = {}
        self._next_fault_publish_retry = 0.0
        self._config_updates = Queue()
        self.service_diagnostics = deque(maxlen=64)

    def queue_config_update(self, config: DLDDConfig) -> None:
        """Hand a runtime configuration update to the primary owner thread."""

        self._config_updates.put(config)

    def _apply_queued_config_updates(self) -> None:
        latest = None
        while True:
            try:
                latest = self._config_updates.get_nowait()
            except Empty:
                break
            self._config_updates.task_done()
        if latest is None:
            return
        self.config = latest
        now = self.clock()
        for identity, deadline in list(self.next_active_recheck.items()):
            self.next_active_recheck[identity] = min(
                deadline, now + latest.active_fault_recheck_interval
            )
        for source in self.source_status.values():
            if source.get("state") == "UNAVAILABLE" and source.get("since") is not None:
                source["grace_deadline"] = (
                    source["since"] + latest.source_unavailable_grace_period
                )
        for identity, record in self.faults.items():
            if record.status == "INACTIVE":
                self._publish_fault_record(
                    identity, record, record.remote_action_time_window
                )

    def process_batch(self, limit: int = 128) -> int:
        processed = 0
        while processed < limit:
            try:
                event = self.evidence_queue.get_nowait()
            except Empty:
                break
            try:
                try:
                    self.process_event(event)
                    self._primary_processing_failures.pop(
                        event.correlation_key, None
                    )
                except Exception as error:
                    self._handle_processing_failure(event, error)
            finally:
                self.evidence_queue.task_done()
            processed += 1
        self.tick()
        return processed

    def _handle_processing_failure(
        self, event: FaultEvidenceEvent, error: Exception
    ) -> None:
        """Isolate a primary-thread failure to the affected work key."""

        LOGGER.exception(
            "unable to process DLDD evidence for %s", event.correlation_key
        )
        count = self._primary_processing_failures.get(event.correlation_key, 0) + 1
        self._primary_processing_failures[event.correlation_key] = count
        fatal = count > self.config.individual_max_failure_threshold
        item = self.work_items.get(event.correlation_key)
        self.broken_rules[event.correlation_key] = {
            "rule": item.rule_name if item is not None else str(event.signature_id),
            "version": item.rule_version if item is not None else "",
            "rule_id": event.signature_id,
            "correlation_key": event.correlation_key,
            "reason": "primary_processing_error: {}".format(error),
            "failure_count": count,
            "state": "BROKEN" if fatal else "DEGRADED",
            "last_attempt": self.wall_clock(),
        }
        self.service_diagnostics.append(
            {
                "reason": "primary_processing_error",
                "rule_id": event.signature_id,
                "component": event.component_name,
                "correlation_key": event.correlation_key,
                "failure_count": count,
                "error": str(error),
                "observed_at": self.wall_clock(),
            }
        )
        try:
            self._command_event(
                event,
                MonitorCommandType.SUSPEND if fatal else MonitorCommandType.RESUME,
                MonitorWorkState.BROKEN if fatal else MonitorWorkState.DEGRADED,
                "primary evidence processing failed",
            )
        except Exception:
            LOGGER.exception(
                "unable to release failed DLDD evidence for %s",
                event.correlation_key,
            )

    def process_event(self, event: FaultEvidenceEvent) -> None:
        result_type = event.result.result
        identity = (event.signature_id, event.component_name)
        reconciliation = self.reconciliation.get(identity)
        if reconciliation is not None and not event.from_recheck:
            self._command_event(
                event,
                MonitorCommandType.HOLD,
                MonitorWorkState.HELD_BY_PRIMARY,
                "reconciliation_evidence_quarantined",
                hold_deadline=self.clock() + self.config.fault_evidence_ack_timeout,
            )
            return
        if reconciliation is not None and event.from_recheck:
            if result_type == EvaluationResultType.SOURCE_RECOVERED:
                self._process_runtime_status(event, release=False)
                reconciliation.recheck_deadline = (
                    self.clock() + self.config.fault_evidence_ack_timeout
                )
                self._command_event(
                    event,
                    MonitorCommandType.RECHECK_ONCE,
                    MonitorWorkState.RECHECK_REQUESTED,
                    "source_recovered_recheck_required",
                    hold_deadline=self.clock()
                    + self.config.fault_evidence_ack_timeout,
                )
                return
            if result_type not in (
                EvaluationResultType.MATCH,
                EvaluationResultType.NO_MATCH,
            ):
                self._process_runtime_status(event, release=False)
            decision = self.correlation.consume(event)
            reconciliation.outstanding_rechecks.discard(event.correlation_key)
            if decision is not None:
                reconciliation.last_decision = decision
            if result_type not in (
                EvaluationResultType.MATCH,
                EvaluationResultType.NO_MATCH,
            ):
                reconciliation.recheck_failed = True
                reconciliation.source_failed = (
                    reconciliation.source_failed
                    or result_type
                    in (
                        EvaluationResultType.SOURCE_UNAVAILABLE,
                        EvaluationResultType.COLLECTION_ERROR,
                    )
                )
            self._command_event(
                event,
                MonitorCommandType.HOLD,
                MonitorWorkState.HELD_BY_PRIMARY,
                "bootstrap_reconciliation_pending",
                hold_deadline=self.clock() + self.config.fault_evidence_ack_timeout,
            )
            if not reconciliation.outstanding_rechecks:
                self._complete_reconciliation(identity, reconciliation)
            return
        pending = self.pending.get(identity)
        if pending is not None and not event.from_recheck:
            self._command_event(
                event,
                MonitorCommandType.HOLD,
                MonitorWorkState.HELD_BY_PRIMARY,
                "local_action_evidence_quarantined",
                hold_deadline=pending.hold_deadline,
            )
            return
        if pending is not None and event.from_recheck:
            if result_type == EvaluationResultType.SOURCE_RECOVERED:
                self._process_runtime_status(event, release=False)
                pending.recheck_deadline = (
                    self.clock() + self.config.fault_evidence_ack_timeout
                )
                self._command_event(
                    event,
                    MonitorCommandType.RECHECK_ONCE,
                    MonitorWorkState.RECHECK_REQUESTED,
                    "source_recovered_recheck_required",
                    hold_deadline=pending.hold_deadline,
                )
                return
            if result_type not in (
                EvaluationResultType.MATCH,
                EvaluationResultType.NO_MATCH,
            ):
                self._process_runtime_status(event, release=False)
            decision = self.correlation.consume(event)
            pending.outstanding_rechecks.discard(event.correlation_key)
            if decision is not None:
                pending.last_decision = decision
            if result_type not in (
                EvaluationResultType.MATCH,
                EvaluationResultType.NO_MATCH,
            ):
                pending.recheck_failed = True
                pending.source_failed = (
                    pending.source_failed
                    or result_type
                    in (
                        EvaluationResultType.SOURCE_UNAVAILABLE,
                        EvaluationResultType.COLLECTION_ERROR,
                    )
                )
            self._command_event(
                event,
                MonitorCommandType.HOLD,
                MonitorWorkState.HELD_BY_PRIMARY,
                "post_action_recheck_pending",
                hold_deadline=pending.hold_deadline,
            )
            if not pending.outstanding_rechecks:
                self._complete_pending(identity, pending)
            return

        if result_type in (
            EvaluationResultType.SOURCE_UNAVAILABLE,
            EvaluationResultType.SOURCE_RECOVERED,
            EvaluationResultType.COLLECTION_ERROR,
            EvaluationResultType.EVALUATION_ERROR,
        ):
            self._process_runtime_status(event)
            return

        self.broken_rules.pop(event.correlation_key, None)

        decision = self.correlation.consume(event)
        if decision is None:
            self._resume(event, "unmapped evidence")
            return
        execution = decision.execution
        actions = execution.signature.actions.repair_actions.local_actions
        if decision.active and decision.changed and actions is not None:
            existing = self.faults.get(identity)
            if (
                existing is not None
                and existing.status == "ACTIVE"
                and existing.action_suppressed
            ):
                self.arbiter.update(decision)
                self._resume(event, "local action already executed for active lifetime")
                return
            if self.action_runner is None:
                completed_at = self.wall_clock()
                future = Future()
                future.dldd_worker_id = ""
                future.set_result(
                    ActionSequenceResult(
                        "",
                        "FAILED",
                        event.event_timestamp,
                        completed_at,
                        (),
                        "action runner unavailable",
                    )
                )
                self._start_pending(decision, future=future)
                return
            self._start_pending(decision)
            return

        if decision.changed:
            artifact = (
                self._request_artifact(execution)
                if decision.active and execution.signature.actions.log_collection
                else None
            )
            self._publish_decision(decision, artifact=artifact)
        self._resume(event, "evidence processed")

    def _process_runtime_status(
        self, event: FaultEvidenceEvent, release: bool = True
    ) -> None:
        result = event.result
        now = self.wall_clock()
        if result.result == EvaluationResultType.EVALUATION_ERROR:
            status = event.runtime_status
            failure_count = status.failure_count if status is not None else 1
            fatal = (
                not result.retryable
                or failure_count > self.config.individual_max_failure_threshold
            )
            self.broken_rules[event.correlation_key] = {
                "rule": status.rule_name if status else str(event.signature_id),
                "version": self.work_items[event.correlation_key].rule_version,
                "rule_id": event.signature_id,
                "correlation_key": event.correlation_key,
                "reason": "{}: {}".format(
                    result.error_category.lower(), result.error
                ),
                "failure_count": failure_count,
                "state": "BROKEN" if fatal else "DEGRADED",
                "last_attempt": now,
            }
            if release:
                self._command_event(
                    event,
                    MonitorCommandType.SUSPEND if fatal else MonitorCommandType.RESUME,
                    MonitorWorkState.BROKEN if fatal else MonitorWorkState.DEGRADED,
                    "fatal evaluator failure" if fatal else "retryable evaluator failure",
                )
            return
        if result.result == EvaluationResultType.SOURCE_RECOVERED:
            failed_keys = self._source_failure_keys.setdefault(event.source_id, set())
            failed_keys.discard(event.correlation_key)
            self.broken_rules.pop(event.correlation_key, None)
            if failed_keys:
                current = dict(self.source_status.get(event.source_id, {}))
                current["affected_rules"] = sorted(
                    {
                        self.work_items[key].rule_id
                        for key in failed_keys
                        if key in self.work_items
                    }
                )
                current["stale_faults"] = self._stale_fault_keys(failed_keys)
                self.source_status[event.source_id] = current
                self._refresh_fault_source_staleness()
                if release:
                    self._command_event(
                        event,
                        MonitorCommandType.RECHECK_ONCE,
                        MonitorWorkState.RECHECK_REQUESTED,
                        "source recovered; evaluate recovered sample",
                        hold_deadline=self.clock()
                        + self.config.fault_evidence_ack_timeout,
                    )
                return
            self._source_failure_keys.pop(event.source_id, None)
            self._suspended_sources.pop(event.source_id, None)
            self._source_unavailable_since.pop(event.source_id, None)
            self.source_status[event.source_id] = {
                "source": event.source_id,
                "state": "RECOVERED",
                "reason": "source produced a valid sample",
                "graceful": False,
                "since": now,
                "failure_count": 0,
                "affected_rules": [event.signature_id],
                "stale_faults": [],
            }
            self._refresh_fault_source_staleness()
            if release:
                self._command_event(
                    event,
                    MonitorCommandType.RECHECK_ONCE,
                    MonitorWorkState.RECHECK_REQUESTED,
                    "source recovered; evaluate recovered sample",
                    hold_deadline=self.clock()
                    + self.config.fault_evidence_ack_timeout,
                )
            return

        first_failure = self._source_unavailable_since.setdefault(event.source_id, now)
        failed_keys = self._source_failure_keys.setdefault(event.source_id, set())
        failed_keys.add(event.correlation_key)
        if self._source_outage_is_expected(event.correlation_key):
            self._suspended_sources[event.source_id] = set(failed_keys)
            self.source_status[event.source_id] = {
                "source": event.source_id,
                "state": "SUSPENDED",
                "reason": "source is in expected platform maintenance",
                "graceful": True,
                "since": first_failure,
                "last_success": (
                    event.runtime_status.last_success_timestamp
                    if event.runtime_status is not None
                    else None
                ),
                "failure_count": (
                    event.runtime_status.failure_count
                    if event.runtime_status is not None
                    else 1
                ),
                "affected_rules": sorted(
                    {
                        self.work_items[key].rule_id
                        for key in failed_keys
                        if key in self.work_items
                    }
                ),
                "stale_faults": self._stale_fault_keys(failed_keys),
            }
            self._refresh_fault_source_staleness()
            if release:
                self._command_event(
                    event,
                    MonitorCommandType.SUSPEND,
                    MonitorWorkState.SUSPENDED,
                    "expected source maintenance",
                )
            return
        within_grace = (
            now - first_failure < self.config.source_unavailable_grace_period
        )
        status = event.runtime_status
        failure_count = status.failure_count if status is not None else 1
        fatal = not result.retryable or (
            not within_grace
            and failure_count > self.config.individual_max_failure_threshold
        )
        state = "BROKEN" if fatal else "DEGRADED"
        self.source_status[event.source_id] = {
            "source": event.source_id,
            "state": "UNAVAILABLE",
            "reason": result.error,
            # Grace is a failure-counting policy, not proof of planned
            # maintenance.  Only an explicit lifecycle integration may mark
            # a source outage graceful.
            "graceful": False,
            "since": first_failure,
            "grace_deadline": first_failure + self.config.source_unavailable_grace_period,
            "last_success": status.last_success_timestamp if status else None,
            "failure_count": failure_count,
            "affected_rules": sorted(
                {
                    self.work_items[key].rule_id
                    for key in failed_keys
                    if key in self.work_items
                }
            ),
            "stale_faults": self._stale_fault_keys(failed_keys),
        }
        if not within_grace or not result.retryable:
            self.broken_rules[event.correlation_key] = {
                "rule": status.rule_name if status else str(event.signature_id),
                "version": self.work_items[event.correlation_key].rule_version,
                "rule_id": event.signature_id,
                "correlation_key": event.correlation_key,
                "reason": "{}: {}".format(result.error_category.lower(), result.error),
                "failure_count": failure_count,
                "state": state,
                "last_attempt": now,
            }
        self._refresh_fault_source_staleness()
        if not release:
            return
        if fatal:
            self._command_event(
                event,
                MonitorCommandType.SUSPEND,
                MonitorWorkState.BROKEN,
                "fatal or threshold-exceeded rule failure",
            )
        else:
            self._command_event(
                event,
                MonitorCommandType.RESUME,
                MonitorWorkState.DEGRADED,
                "retryable rule or source failure",
            )

    def _stale_fault_keys(self, failed_keys: Set[str]):
        stale = []
        for identity, fault in self.faults.items():
            if fault.status != "ACTIVE":
                continue
            execution = self.correlation.executions.get(identity)
            if execution is not None and failed_keys.intersection(
                self._execution_keys(execution)
            ):
                stale.append(fault.redis_key)
        return stale

    def _source_outage_is_expected(
        self, key: str, preserve_on_error: bool = False
    ) -> bool:
        if self.source_lifecycle_probe is None:
            return False
        try:
            return bool(self.source_lifecycle_probe(self.work_items[key]))
        except Exception:
            return preserve_on_error

    def _recover_expected_source_suspensions(self) -> None:
        now = self.clock()
        if now < self._next_source_lifecycle_probe:
            return
        self._next_source_lifecycle_probe = now + 5.0
        for source_id, keys in list(self._suspended_sources.items()):
            if not keys:
                self._suspended_sources.pop(source_id, None)
                continue
            if any(
                self._source_outage_is_expected(key, preserve_on_error=True)
                for key in keys
            ):
                continue
            self._suspended_sources.pop(source_id, None)
            self._source_unavailable_since[source_id] = self.wall_clock()
            current = dict(self.source_status.get(source_id, {}))
            current.update(
                {
                    "state": "UNAVAILABLE",
                    "reason": "expected maintenance ended; awaiting successful sample",
                    "graceful": False,
                    "since": self.wall_clock(),
                    "grace_deadline": self.wall_clock()
                    + self.config.source_unavailable_grace_period,
                }
            )
            self.source_status[source_id] = current
            for key in keys:
                self._command_key(
                    key,
                    MonitorCommandType.RESUME,
                    MonitorWorkState.DEGRADED,
                    "expected source maintenance ended",
                )

    def _start_pending(
        self, decision: CorrelationDecision, future: Optional[Future] = None
    ) -> None:
        execution = decision.execution
        identity = (execution.signature.metadata.id, execution.component_name)
        local = execution.signature.actions.repair_actions.local_actions
        actions = tuple(self._operation_payload(item) for item in local.action_list)
        if future is None:
            future = self.action_runner.submit(
                execution.signature.metadata.name,
                actions,
                self.local_action_default_timeout,
            )
        max_timeout = sum(
            float(item.get("timeout", self.local_action_default_timeout or 0))
            for item in actions
        )
        hold_budget = (
            max_timeout
            + local.wait_period
            + (2 * self.config.fault_evidence_ack_timeout)
            + 60
        )
        pending = PendingFault(
            execution=execution,
            first_decision=decision,
            future=future,
            hold_deadline=self.clock() + hold_budget,
            action_deadline=self.clock() + max_timeout + 30,
        )
        self.pending[identity] = pending
        # The signature is already correlated active even though its own
        # publication gate is still closed.  Include it in arbitration so a
        # lower-severity signature cannot temporarily claim the singular
        # component/symptom FAULT_INFO row while remediation is in progress.
        self.arbiter.update(decision)
        self._record_candidate(decision, pending)
        for key in self._execution_keys(execution):
            self._command_key(
                key,
                MonitorCommandType.HOLD,
                MonitorWorkState.HELD_BY_PRIMARY,
                "local_action_running",
                hold_deadline=pending.hold_deadline,
                evidence=decision.event if key == decision.event.correlation_key else None,
            )

    def _record_candidate(
        self, decision: CorrelationDecision, pending: PendingFault
    ) -> None:
        """Keep action candidates process-local until their final recheck."""

        execution = decision.execution
        metadata = execution.signature.metadata
        identity = (metadata.id, execution.component_name)
        previous = self.faults.get(identity)
        occurrences = 1
        if previous is not None:
            occurrences = previous.occurrences + (
                1 if previous.status == "INACTIVE" else 0
            )
        worker_id = getattr(pending.future, "dldd_worker_id", "")
        details = {
            "state": "RUNNING",
            "correlation_key": decision.event.correlation_key,
            "worker_id": worker_id,
            "started_at": decision.event.event_timestamp,
            "completed_at": None,
            "action_suppressed": True,
            "last_error": "",
        }
        self.faults[identity] = FaultRecord(
            rule_id=metadata.id,
            rule_name=metadata.name,
            rule_version=metadata.version,
            schema_version="0.0.1",
            active_rules_checksum=self.active_rules_checksum,
            component_type=metadata.component,
            component_name=execution.component_name,
            symptom=metadata.symptom,
            severity=metadata.severity,
            priority=metadata.priority,
            error_type=metadata.error_type,
            description=metadata.description,
            status="CANDIDATE",
            origin_time=decision.event.event_timestamp,
            last_detection_time=decision.event.event_timestamp,
            occurrences=occurrences,
            events=decision.event_snapshots,
            local_action_state="RUNNING",
            local_action_details=details,
            action_suppressed=True,
        )

    def tick(self) -> None:
        self._apply_queued_config_updates()
        now = self.clock()
        self._retry_dirty_faults()
        self._refresh_artifact_states()
        self._recover_expected_source_suspensions()
        for identity, pending in list(self.pending.items()):
            if pending.phase == "ACTIONS" and pending.future.done():
                try:
                    pending.action_result = pending.future.result()
                except Exception as error:
                    now_wall = self.wall_clock()
                    pending.action_result = ActionSequenceResult(
                        getattr(pending.future, "dldd_worker_id", ""),
                        "FAILED",
                        pending.first_decision.event.event_timestamp,
                        now_wall,
                        (),
                        str(error),
                    )
                pending.artifact = self._request_artifact(pending.execution)
                wait_period = (
                    pending.execution.signature.actions.repair_actions.local_actions.wait_period
                )
                pending.wait_until = now + wait_period
                pending.phase = "WAITING_FOR_RECHECK"
            elif pending.phase == "ACTIONS" and now >= pending.action_deadline:
                now_wall = self.wall_clock()
                pending.action_result = ActionSequenceResult(
                    getattr(pending.future, "dldd_worker_id", ""),
                    "FAILED",
                    pending.first_decision.event.event_timestamp,
                    now_wall,
                    (),
                    "action worker did not complete before the action deadline",
                )
                pending.artifact = self._request_artifact(pending.execution)
                wait_period = (
                    pending.execution.signature.actions.repair_actions.local_actions.wait_period
                )
                pending.wait_until = now + wait_period
                pending.phase = "WAITING_FOR_RECHECK"
                self.service_diagnostics.append(
                    {
                        "reason": "local_action_deadline_expired",
                        "rule_id": identity[0],
                        "component": identity[1],
                        "observed_at": now_wall,
                    }
                )
            if (
                pending.phase == "WAITING_FOR_RECHECK"
                and pending.wait_until is not None
                and now >= pending.wait_until
            ):
                pending.outstanding_rechecks = set(
                    self._execution_keys(pending.execution)
                )
                pending.phase = "RECHECKING"
                pending.recheck_attempts = 1
                pending.recheck_deadline = (
                    now + self.config.fault_evidence_ack_timeout
                )
                for key in pending.outstanding_rechecks:
                    self._command_key(
                        key,
                        MonitorCommandType.RECHECK_ONCE,
                        MonitorWorkState.RECHECK_REQUESTED,
                        "post_action_recheck",
                        hold_deadline=pending.hold_deadline,
                    )
            if (
                pending.phase == "RECHECKING"
                and pending.outstanding_rechecks
                and pending.recheck_deadline is not None
                and now >= pending.recheck_deadline
            ):
                if pending.recheck_attempts < 2 and now < pending.hold_deadline:
                    pending.recheck_attempts += 1
                    pending.recheck_deadline = (
                        now + self.config.fault_evidence_ack_timeout
                    )
                    for key in pending.outstanding_rechecks:
                        self._command_key(
                            key,
                            MonitorCommandType.RECHECK_ONCE,
                            MonitorWorkState.RECHECK_REQUESTED,
                            "post_action_recheck_retry",
                            hold_deadline=pending.hold_deadline,
                        )
                else:
                    pending.recheck_failed = True
                    pending.source_failed = True
                    self.service_diagnostics.append(
                        {
                            "reason": "post_action_recheck_timed_out",
                            "rule_id": identity[0],
                            "component": identity[1],
                            "state": "ACTIVE",
                            "observed_at": self.wall_clock(),
                        }
                    )
                    self._complete_pending(identity, pending)

        for identity, reconciliation in list(self.reconciliation.items()):
            if (
                reconciliation.outstanding_rechecks
                and now >= reconciliation.recheck_deadline
            ):
                if (
                    reconciliation.recheck_attempts < 2
                    and now < reconciliation.hold_deadline
                ):
                    reconciliation.recheck_attempts += 1
                    reconciliation.recheck_deadline = (
                        now + self.config.fault_evidence_ack_timeout
                    )
                    for key in reconciliation.outstanding_rechecks:
                        self._command_key(
                            key,
                            MonitorCommandType.RECHECK_ONCE,
                            MonitorWorkState.RECHECK_REQUESTED,
                            "{}_retry".format(reconciliation.reason),
                            hold_deadline=reconciliation.hold_deadline,
                        )
                else:
                    reconciliation.recheck_failed = True
                    reconciliation.source_failed = True
                    self.service_diagnostics.append(
                        {
                            "reason": "{}_timed_out".format(
                                reconciliation.reason
                            ),
                            "rule_id": identity[0],
                            "component": identity[1],
                            "state": "ACTIVE",
                            "observed_at": self.wall_clock(),
                        }
                    )
                    self._complete_reconciliation(identity, reconciliation)

        for identity, deadline in list(self.next_active_recheck.items()):
            if now < deadline or identity in self.pending or identity in self.reconciliation:
                continue
            record = self.faults.get(identity)
            execution = self.correlation.executions.get(identity)
            if record is None or execution is None or record.status != "ACTIVE":
                self.next_active_recheck.pop(identity, None)
                continue
            self._start_reconciliation(
                execution, record, "active_fault_periodic_recheck"
            )

    def _refresh_artifact_states(self) -> None:
        if self.artifact_client is None:
            return
        for identity, record in self.faults.items():
            artifact = record.healthz_artifact
            if not artifact or artifact.get("state") not in ("REQUESTED", "RUNNING"):
                continue
            artifact_id = artifact.get("artifact_id")
            if not artifact_id:
                continue
            try:
                current = self.artifact_client.status(artifact_id).as_payload()
            except Exception:
                continue
            if current == artifact:
                continue
            record.healthz_artifact = current
            execution = self.correlation.executions.get(identity)
            remote_window = record.remote_action_time_window
            if execution is not None:
                remote_window = (
                    execution.signature.actions.repair_actions.remote_actions.time_window
                )
            self._publish_fault_record(identity, record, remote_window)

    def _complete_pending(self, identity, pending: PendingFault) -> None:
        decision = pending.last_decision or pending.first_decision
        active = decision.active or pending.recheck_failed
        # An unavailable recheck never clears a previously confirmed fault.
        if decision.event.result.result not in (
            EvaluationResultType.MATCH,
            EvaluationResultType.NO_MATCH,
        ):
            active = True
        final_decision = CorrelationDecision(
            decision.execution,
            active,
            True,
            (
                decision.event_snapshots
                if active and decision.event_snapshots
                else pending.first_decision.event_snapshots
            ),
            decision.event,
        )
        action_state = pending.action_result.state if pending.action_result else "FAILED"
        actions_taken = (
            tuple(item.as_payload() for item in pending.action_result.actions)
            if pending.action_result
            else ()
        )
        action_details = {
            "state": action_state,
            "correlation_key": pending.first_decision.event.correlation_key,
            "worker_id": (
                pending.action_result.worker_id
                if pending.action_result is not None
                else getattr(pending.future, "dldd_worker_id", "")
            ),
            "started_at": (
                pending.action_result.started_at
                if pending.action_result is not None
                else pending.first_decision.event.event_timestamp
            ),
            "completed_at": (
                pending.action_result.completed_at
                if pending.action_result is not None
                else self.wall_clock()
            ),
            "action_suppressed": True,
            "last_error": (
                pending.action_result.last_error
                if pending.action_result is not None
                else "action result unavailable"
            ),
        }
        if action_state == "FAILED":
            LOGGER.error(
                "local action sequence failed for rule %s component %s: %s",
                identity[0],
                identity[1],
                action_details["last_error"],
            )
            self.service_diagnostics.append(
                {
                    "reason": "local_action_failed",
                    "rule_id": identity[0],
                    "component": identity[1],
                    "worker_id": action_details["worker_id"],
                    "error": action_details["last_error"],
                    "observed_at": self.wall_clock(),
                }
            )
        uncertain = pending.source_failed or pending.recheck_failed
        if uncertain:
            self.uncertain_faults.add(identity)
        else:
            self.uncertain_faults.discard(identity)
        self._publish_decision(
            final_decision,
            local_action_state=action_state,
            local_action_details=action_details,
            actions_taken=actions_taken,
            artifact=pending.artifact,
            action_suppressed=True,
            stale_source=uncertain,
        )
        for key in self._execution_keys(pending.execution):
            self._release_key(key, "post_action_lifecycle_complete")
        self.pending.pop(identity, None)

    def reconcile_existing_faults(self) -> None:
        """Recheck current-generation active records before normal publication."""

        for payload in self.telemetry.read_faults():
            if not self._is_dldd_fault_payload(payload):
                continue
            try:
                record = self._fault_from_payload(payload)
            except (AttributeError, TypeError, ValueError) as error:
                self.service_diagnostics.append(
                    {
                        "reason": "malformed_persisted_fault_skipped",
                        "redis_key": str(payload.get("redis_key", "")),
                        "error": str(error),
                        "observed_at": self.wall_clock(),
                    }
                )
                continue
            identity = (record.rule_id, record.component_name)
            execution = self.correlation.executions.get(identity)
            current = (
                execution is not None
                and record.schema_version == "0.0.1"
                and record.active_rules_checksum == self.active_rules_checksum
                and execution.signature.metadata.symptom == record.symptom
            )
            if record.status != "ACTIVE":
                # Retained inactive rows own occurrence history even if a new
                # generation replaces the rule that originally produced them.
                self.faults[identity] = record
                self.published_by_key[
                    (record.component_name, record.symptom)
                ] = record.rule_id
                continue
            if not current:
                record.status = "INACTIVE"
                record.repair_actions = ()
                record.last_detection_time = self.wall_clock()
                record.description = "{} [stale rule/source after DLDD restart]".format(
                    record.description
                ).strip()
                self.faults[identity] = record
                self.published_by_key[
                    (record.component_name, record.symptom)
                ] = record.rule_id
                self._publish_fault_record(
                    identity, record, record.remote_action_time_window
                )
                continue
            self.faults[identity] = record
            self.published_by_key[(record.component_name, record.symptom)] = record.rule_id
            self._start_reconciliation(
                execution, record, "bootstrap_fault_reconciliation"
            )

    def _start_reconciliation(
        self, execution: SignatureExecution, record: FaultRecord, reason: str
    ) -> None:
        identity = (execution.signature.metadata.id, execution.component_name)
        keys = set(self._execution_keys(execution))
        now = self.clock()
        deadline = now + self.config.fault_evidence_ack_timeout
        hold_deadline = (
            now + (2 * self.config.fault_evidence_ack_timeout) + 30
        )
        self.reconciliation[identity] = Reconciliation(
            execution,
            record,
            keys,
            reason=reason,
            hold_deadline=hold_deadline,
            recheck_deadline=deadline,
        )
        for key in keys:
            self._command_key(
                key,
                MonitorCommandType.RECHECK_ONCE,
                MonitorWorkState.RECHECK_REQUESTED,
                reason,
                hold_deadline=hold_deadline,
            )

    def _complete_reconciliation(self, identity, reconciliation) -> None:
        decision = reconciliation.last_decision
        record = reconciliation.record
        state_changed = False
        uncertain = reconciliation.source_failed or reconciliation.recheck_failed
        if uncertain:
            self.uncertain_faults.add(identity)
        else:
            self.uncertain_faults.discard(identity)
        if decision is None:
            active = True
            self.correlation.set_active(identity[0], identity[1], True)
            self.arbiter.restore_active(
                reconciliation.execution,
                reconciliation.record.origin_time,
            )
            self.next_active_recheck[identity] = (
                self.clock() + self.config.active_fault_recheck_interval
            )
        else:
            active = decision.active or reconciliation.recheck_failed
            self.correlation.set_active(identity[0], identity[1], active)
            state_changed = active != (record.status == "ACTIVE")
            effective = CorrelationDecision(
                reconciliation.execution,
                active,
                state_changed,
                decision.event_snapshots or record.events,
                decision.event,
            )
            if state_changed:
                self._publish_decision(
                    effective,
                    local_action_state=record.local_action_state,
                    local_action_details=record.local_action_details,
                    actions_taken=record.actions_taken,
                    artifact=record.healthz_artifact,
                    action_suppressed=record.action_suppressed,
                    stale_source=uncertain,
                )
            elif active:
                # A recheck that confirms the same state must not rewrite the
                # state-transition timestamp.
                self.arbiter.update(effective)
                self.next_active_recheck[identity] = (
                    self.clock() + self.config.active_fault_recheck_interval
                )
        if not state_changed:
            stale_before = record.stale_source
            record.stale_source = uncertain or self._execution_has_failed_source(
                reconciliation.execution
            )
            if record.stale_source != stale_before:
                self._publish_fault_record(
                    identity, record, record.remote_action_time_window
                )
        self._refresh_fault_source_staleness()
        for key in self._execution_keys(reconciliation.execution):
            self._release_key(key, "{}_complete".format(reconciliation.reason))
        self.service_diagnostics.append(
            {
                "reason": "{}_complete".format(reconciliation.reason),
                "rule_id": identity[0],
                "component": identity[1],
                "state": "ACTIVE" if active else "INACTIVE",
                "source_stale": uncertain,
                "observed_at": self.wall_clock(),
            }
        )
        self.reconciliation.pop(identity, None)

    @staticmethod
    def _is_dldd_fault_payload(payload: Mapping[str, Any]) -> bool:
        """Do not reconcile or rewrite FAULT_INFO rows owned by another agent."""

        try:
            rule_id = int(payload.get("rule_id", 0))
        except (TypeError, ValueError):
            return False
        return bool(
            rule_id
            and payload.get("rule")
            and payload.get("schema_version")
            and payload.get("active_rules_checksum")
        )

    @staticmethod
    def _fault_from_payload(payload: Mapping[str, Any]) -> FaultRecord:
        component = payload.get("component_info") or {}
        local = payload.get("local_action_state") or {}
        repairs = payload.get("repair_actions") or []
        return FaultRecord(
            rule_id=int(payload.get("rule_id", 0)),
            rule_name=str(payload.get("rule", "")),
            rule_version=str(payload.get("rule_version", "")),
            schema_version=str(payload.get("schema_version", "")),
            active_rules_checksum=str(payload.get("active_rules_checksum", "")),
            component_type=str(component.get("component", "")),
            component_name=str(component.get("name", "")),
            symptom=str(payload.get("symptom", "")),
            severity=str(payload.get("severity", "UNKNOWN")),
            priority=int(payload.get("priority", 5)),
            error_type=str(payload.get("error_type", "")),
            description=str(payload.get("description", "")),
            status=str(payload.get("status", "ACTIVE")),
            origin_time=float(payload.get("origin_time", time.time())),
            last_detection_time=float(payload.get("last_detection_time", time.time())),
            occurrences=int(payload.get("occurrences", 1)),
            events=tuple(payload.get("events") or ()),
            repair_actions=tuple(
                item.get("action") for item in repairs if item.get("action")
            ),
            remote_action_time_window=int(
                payload.get("remote_action_time_window", 0)
            ),
            actions_taken=tuple(payload.get("actions_taken") or ()),
            local_action_state=str(local.get("state", "IDLE")),
            local_action_details=dict(local),
            action_suppressed=bool(local.get("action_suppressed", False)),
            healthz_artifact=payload.get("healthz_artifact"),
            serial_number=str(component.get("serial_number", "")),
            stale_source=bool(payload.get("source_stale", False)),
        )

    def _publish_decision(
        self,
        decision: CorrelationDecision,
        local_action_state: str = "IDLE",
        local_action_details=None,
        actions_taken=(),
        artifact=None,
        action_suppressed: bool = False,
        stale_source: bool = False,
    ) -> None:
        execution = decision.execution
        metadata = execution.signature.metadata
        identity = (metadata.id, execution.component_name)
        existing = self.faults.get(identity)
        now = decision.event.event_timestamp
        status = "ACTIVE" if decision.active else "INACTIVE"
        state_changed = existing is None or existing.status != status
        if existing is None:
            existing = FaultRecord(
                rule_id=metadata.id,
                rule_name=metadata.name,
                rule_version=metadata.version,
                schema_version="0.0.1",
                active_rules_checksum=self.active_rules_checksum,
                component_type=metadata.component,
                component_name=execution.component_name,
                symptom=metadata.symptom,
                severity=metadata.severity,
                priority=metadata.priority,
                error_type=metadata.error_type,
                description=metadata.description,
                status=status,
                origin_time=now,
                last_detection_time=now,
            )
            self.faults[identity] = existing
        elif existing.status == "INACTIVE" and status == "ACTIVE":
            existing.occurrences += 1
            existing.origin_time = now
        preserve_action_history = (
            status == "INACTIVE"
            and existing.local_action_state not in ("", "IDLE")
            and local_action_state == "IDLE"
            and not actions_taken
        )
        existing.status = status
        if state_changed:
            existing.last_detection_time = now
        if decision.event_snapshots:
            existing.events = decision.event_snapshots
        if not preserve_action_history:
            existing.actions_taken = tuple(actions_taken)
            existing.local_action_state = local_action_state
            existing.local_action_details = dict(
                local_action_details
                or {
                    "state": local_action_state,
                    "correlation_key": decision.event.correlation_key,
                    "action_suppressed": action_suppressed,
                    "last_error": "",
                }
            )
            existing.action_suppressed = action_suppressed
        if artifact is not None:
            existing.healthz_artifact = artifact
        failed_keys = {
            key
            for source_keys in self._source_failure_keys.values()
            for key in source_keys
        }
        existing.stale_source = stale_source or bool(
            failed_keys.intersection(self._execution_keys(execution))
        )
        remote = execution.signature.actions.repair_actions.remote_actions
        existing.repair_actions = remote.action_list if status == "ACTIVE" else ()
        existing.remote_action_time_window = remote.time_window
        if status == "ACTIVE":
            self.next_active_recheck[identity] = (
                self.clock() + self.config.active_fault_recheck_interval
            )
        else:
            self.next_active_recheck.pop(identity, None)

        winner = self.arbiter.update(decision)
        fault_key = (execution.component_name, metadata.symptom)
        if status == "ACTIVE" and winner is not None:
            if winner.signature.metadata.id != metadata.id:
                return
            previous_rule = self.published_by_key.get(fault_key)
            if previous_rule is not None and previous_rule != metadata.id:
                previous = self.faults.get((previous_rule, execution.component_name))
                if previous is not None:
                    if previous.status == "INACTIVE":
                        existing.occurrences = previous.occurrences + 1
                    else:
                        existing.origin_time = previous.origin_time
                        existing.occurrences = previous.occurrences
            self.published_by_key[fault_key] = metadata.id
        elif status == "INACTIVE":
            published_rule = self.published_by_key.get(fault_key)
            if published_rule is not None and published_rule != metadata.id:
                return
            if winner is not None:
                winner_id = winner.signature.metadata.id
                alternate = self.faults.get((winner_id, execution.component_name))
                if alternate is not None:
                    alternate.origin_time = existing.origin_time
                    alternate.occurrences = existing.occurrences
                    self.published_by_key[fault_key] = winner_id
                    alternate_remote = winner.signature.actions.repair_actions.remote_actions
                    alternate.local_action_details = {
                        **dict(alternate.local_action_details),
                        "state": alternate.local_action_state,
                        "action_suppressed": alternate.action_suppressed,
                    }
                    self._publish_fault_record(
                        (winner_id, execution.component_name),
                        alternate,
                        alternate_remote.time_window,
                    )
                    return
            # The retained inactive row remains the owner of the Redis key so
            # a later competing signature can inherit occurrence history.
            self.published_by_key[fault_key] = metadata.id

        self._publish_fault_record(identity, existing, remote.time_window)

    def _refresh_fault_source_staleness(self) -> None:
        failed_keys = {
            key
            for source_keys in self._source_failure_keys.values()
            for key in source_keys
        }
        for identity, record in self.faults.items():
            if record.status != "ACTIVE":
                continue
            execution = self.correlation.executions.get(identity)
            stale = bool(
                identity in self.uncertain_faults
                or (
                    execution is not None
                    and failed_keys.intersection(self._execution_keys(execution))
                )
            )
            if record.stale_source == stale:
                continue
            record.stale_source = stale
            remote_window = record.remote_action_time_window
            if execution is not None:
                remote_window = (
                    execution.signature.actions.repair_actions.remote_actions.time_window
                )
            self._publish_fault_record(identity, record, remote_window)

    def _execution_has_failed_source(self, execution: SignatureExecution) -> bool:
        failed_keys = {
            key
            for source_keys in self._source_failure_keys.values()
            for key in source_keys
        }
        return bool(failed_keys.intersection(self._execution_keys(execution)))

    def _publish_fault_record(
        self,
        identity: Tuple[int, str],
        record: FaultRecord,
        remote_window: int,
    ) -> bool:
        owner = self.published_by_key.get(
            (record.component_name, record.symptom)
        )
        if owner is not None and owner != record.rule_id:
            # Suppressed signatures retain internal/action/artifact state but
            # must never overwrite the one component/symptom Redis row owned
            # by the arbiter winner.
            self.dirty_faults.discard(identity)
            return True
        record.remote_action_time_window = remote_window
        published = self.telemetry.publish_fault(
            record,
            remote_action_time_window=remote_window,
            local_action_details=record.local_action_details,
        )
        if published:
            self.dirty_faults.discard(identity)
        else:
            self.dirty_faults.add(identity)
        return published

    def _retry_dirty_faults(self) -> None:
        now = self.clock()
        if now < self._next_fault_publish_retry:
            return
        self._next_fault_publish_retry = now + 5.0
        for identity in tuple(self.dirty_faults):
            record = self.faults.get(identity)
            if record is None or record.status == "CANDIDATE":
                self.dirty_faults.discard(identity)
                continue
            execution = self.correlation.executions.get(identity)
            remote_window = record.remote_action_time_window
            if execution is not None:
                remote_window = (
                    execution.signature.actions.repair_actions.remote_actions.time_window
                )
            self._publish_fault_record(identity, record, remote_window)

    def _request_artifact(self, execution: SignatureExecution):
        collection = execution.signature.actions.log_collection
        if collection is None or self.artifact_client is None:
            return None
        try:
            requested_at = self.wall_clock()
            request = self.artifact_client.request(
                {
                    "rule": execution.signature.metadata.name,
                    "rule_id": execution.signature.metadata.id,
                    "timestamp": requested_at,
                    "component_info": {
                        "component": execution.signature.metadata.component,
                        "name": execution.component_name,
                    },
                    "symptom": execution.signature.metadata.symptom,
                },
                collection.logs,
                tuple(self._operation_payload(item) for item in collection.queries),
            )
            return request.as_payload()
        except Exception as error:
            return {
                "state": "FAILED",
                "requested_at": self.wall_clock(),
                "completed_at": self.wall_clock(),
                "last_error": str(error),
            }

    @staticmethod
    def _operation_payload(operation) -> Mapping[str, Any]:
        # Vendor metadata is additive.  It must never replace the typed fields
        # that validation/materialization selected for runtime dispatch.
        reserved = {
            "type",
            "command",
            "argv",
            "path",
            "timeout",
            "max_output_bytes",
            "executor",
        }
        payload = {
            key: value
            for key, value in operation.options.items()
            if key not in reserved
        }
        payload["type"] = operation.type
        if operation.command is not None:
            payload["command"] = operation.command
        if operation.argv:
            payload["argv"] = list(operation.argv)
        if operation.path:
            payload["path"] = dict(operation.path)
        if operation.timeout is not None:
            payload["timeout"] = operation.timeout
        if operation.max_output_bytes is not None:
            payload["max_output_bytes"] = operation.max_output_bytes
        if operation.executor is not None:
            payload["executor"] = operation.executor
        return payload

    @staticmethod
    def _execution_keys(execution: SignatureExecution):
        return tuple(
            key
            for event_keys in execution.event_keys.values()
            for key in event_keys
        )

    def _resume(self, event: FaultEvidenceEvent, reason: str) -> None:
        self._command_event(
            event,
            MonitorCommandType.RESUME,
            MonitorWorkState.READY,
            reason,
        )

    def _release_key(self, key: str, reason: str) -> None:
        broken = self.broken_rules.get(key)
        if broken is not None and broken.get("state") == "BROKEN":
            self._command_key(
                key,
                MonitorCommandType.SUSPEND,
                MonitorWorkState.BROKEN,
                reason,
            )
            return
        item = self.work_items[key]
        source_state = self.source_status.get(item.source_id, {}).get("state")
        if source_state == "SUSPENDED":
            self._command_key(
                key,
                MonitorCommandType.SUSPEND,
                MonitorWorkState.SUSPENDED,
                reason,
            )
            return
        unavailable = source_state == "UNAVAILABLE"
        self._command_key(
            key,
            MonitorCommandType.RESUME,
            MonitorWorkState.DEGRADED if unavailable else MonitorWorkState.READY,
            reason,
        )

    def _command_event(self, event, command, target, reason, **kwargs) -> None:
        self._command_key(
            event.correlation_key,
            command,
            target,
            reason,
            evidence=event,
            **kwargs
        )

    def _command_key(
        self,
        key: str,
        command: MonitorCommandType,
        target: MonitorWorkState,
        reason: str,
        evidence: Optional[FaultEvidenceEvent] = None,
        **kwargs
    ) -> None:
        item = self.work_items[key]
        monitor_type = (
            "redis"
            if item.source_type == "redis"
            else "file"
            if item.source_type == "file"
            else "common"
        )
        plan = self.plans[monitor_type]
        plan.control_queue.put(
            MonitorControlCommand(
                command_id=str(uuid.uuid4()),
                monitor_id=plan.monitor_id,
                plan_generation=plan.plan_generation,
                correlation_key=key,
                command=command,
                target_state=target,
                reason=reason,
                expected_work_state_generation=(
                    evidence.work_state_generation if evidence is not None else None
                ),
                evidence_sequence=evidence.sequence if evidence is not None else None,
                recheck_not_before=kwargs.get("recheck_not_before"),
                hold_deadline=kwargs.get("hold_deadline"),
            )
        )

    def service_state(self) -> str:
        broken_rule_ids = {
            item.get("rule_id", item.get("rule"))
            for item in self.broken_rules.values()
            if item.get("state") == "BROKEN"
        }
        if len(broken_rule_ids) > self.config.broken_rules_max_threshold:
            return "BROKEN|FATAL"
        if self.broken_rules or any(
            item.get("state") in ("UNAVAILABLE", "SUSPENDED")
            for item in self.source_status.values()
        ):
            return "DEGRADED"
        return "OK"
