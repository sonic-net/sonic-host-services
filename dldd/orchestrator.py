"""Primary-thread evidence processing and fault lifecycle coordination."""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Dict, Mapping, Optional, Set, Tuple, Union

from .actions import ActionRunner, ActionSequenceResult
from .artifacts import HealthzArtifactClient
from .config import DLDDConfig
from .correlation import CorrelationDecision, CorrelationEngine, FaultArbiter, SignatureExecution
from .ownership import is_dldd_fault_payload
from .planner import monitor_type_for_source
from .rule_schema.errors import bound_diagnostic
from .runtime import (
    DSEExpansionEvent,
    EvaluationResultType,
    FaultEvidenceEvent,
    FaultRecord,
    MonitorCommandType,
    MonitorControlCommand,
    MonitorExecutionPlan,
    MonitorWorkState,
    make_rule_instance_id,
)
from .telemetry import TelemetryPublisher
from .timestamps import floor_timestamp_fields


LOGGER = logging.getLogger(__name__)


_COMMAND_BY_TARGET = {
    MonitorWorkState.READY: MonitorCommandType.RESUME,
    MonitorWorkState.DEGRADED: MonitorCommandType.RESUME,
    MonitorWorkState.HELD_BY_PRIMARY: MonitorCommandType.HOLD,
    MonitorWorkState.RECHECK_REQUESTED: MonitorCommandType.RECHECK_ONCE,
    MonitorWorkState.BROKEN: MonitorCommandType.SUSPEND,
    MonitorWorkState.SUSPENDED: MonitorCommandType.SUSPEND,
}
_DECISIVE_RECHECK_RESULTS = frozenset(
    (EvaluationResultType.MATCH, EvaluationResultType.NO_MATCH)
)
_MAX_RECHECK_ATTEMPTS = 2


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
    recheck_failed: bool = False
    recheck_deadline: Optional[float] = None
    recheck_attempts: int = 0


@dataclass
class Reconciliation:
    execution: SignatureExecution
    outstanding_rechecks: Set[str]
    last_decision: Optional[CorrelationDecision] = None
    recheck_failed: bool = False
    reason: str = "bootstrap_fault_reconciliation"
    hold_deadline: float = 0.0
    recheck_deadline: float = 0.0
    recheck_attempts: int = 1


class PrimaryOrchestrator:
    """Own correlation, recovery, actions, artifacts, and fault publication."""

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
        self._plan_by_work_key: Dict[str, MonitorExecutionPlan] = {}
        for plan in self.plans.values():
            for key in plan.item_snapshot():
                self._register_plan_owner(key, plan)
        self.work_items = dict(work_items)
        self.dynamic_signatures = {
            template.item.rule_id: template.signature
            for plan in self.plans.values()
            for template in plan.templates_by_key.values()
        }
        self._dse_template_ids_by_rule: Dict[int, Set[str]] = {}
        for plan in self.plans.values():
            for template_id, template in plan.templates_by_key.items():
                self._dse_template_ids_by_rule.setdefault(
                    template.item.rule_id, set()
                ).add(template_id)
        self._dse_instances_by_template: Dict[str, Set[str]] = {}
        self._dse_retirement_candidates: Set[Tuple[int, str]] = set()
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
        self.source_status: Dict[str, Dict[str, Any]] = {}
        self._source_unavailable_since: Dict[str, float] = {}
        self._source_failure_keys: Dict[str, Set[str]] = {}
        self._suspended_sources: Dict[str, Set[str]] = {}
        self._next_source_lifecycle_probe = 0.0
        self.reconciliation: Dict[Tuple[int, str], Reconciliation] = {}
        self.pending_dynamic_faults: Dict[
            Tuple[int, str], FaultRecord
        ] = {}
        self.next_active_recheck: Dict[Tuple[int, str], float] = {}
        self.uncertain_faults: Set[Tuple[int, str]] = set()
        self.dirty_faults: Set[Tuple[int, str]] = set()
        self._primary_processing_failures: Dict[str, int] = {}
        self._next_fault_publish_retry = 0.0
        self._config_updates: Queue[DLDDConfig] = Queue()
        self.service_diagnostics: deque[Dict[str, Any]] = deque(maxlen=64)

    def _register_plan_owner(
        self, key: str, plan: MonitorExecutionPlan
    ) -> None:
        existing = self._plan_by_work_key.get(key)
        if existing is not None and existing is not plan:
            raise ValueError(
                "work key {!r} is assigned to multiple monitors".format(key)
            )
        self._plan_by_work_key[key] = plan

    def queue_config_update(self, config: DLDDConfig) -> None:
        """Hand a runtime configuration update to the primary owner thread."""

        self._config_updates.put(config)

    def restore_broken_work(self, key: str, record: Mapping[str, Any]) -> None:
        """Restore one persisted broken work item before monitors start."""

        self.broken_rules[key] = record
        self._command_key(
            key,
            MonitorWorkState.BROKEN,
            "restored broken rule after unclean restart",
        )

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
        self.telemetry.config = latest
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
        for _identity, record in self.faults.items():
            if record.status == "INACTIVE":
                record.inactive_deadline = (
                    self.wall_clock()
                    + latest.inactive_fault_retention_period
                )
                self._publish_fault_record(record)

    def process_batch(self, limit: int = 128) -> int:
        processed = 0
        while processed < limit:
            try:
                event = self.evidence_queue.get_nowait()
            except Empty:
                break
            try:
                try:
                    if isinstance(event, DSEExpansionEvent):
                        self.process_expansion(event)
                    else:
                        self.process_event(event)
                        self._primary_processing_failures.pop(
                            event.correlation_key, None
                        )
                except Exception as error:
                    if isinstance(event, DSEExpansionEvent):
                        LOGGER.exception(
                            "unable to apply DSE expansion %s",
                            event.template_id,
                        )
                        self.service_diagnostics.append(
                            {
                                "reason": "dse_expansion_registration_failed",
                                "template_id": event.template_id,
                                "error": str(error),
                                "observed_at": self.wall_clock(),
                            }
                        )
                    else:
                        self._handle_processing_failure(event, error)
            finally:
                self.evidence_queue.task_done()
            processed += 1
        self.tick()
        return processed

    def process_expansion(self, event: DSEExpansionEvent) -> None:
        plan = self.plans.get("common")
        if (
            plan is None
            or event.monitor_id != plan.monitor_id
            or event.plan_generation != plan.plan_generation
        ):
            raise ValueError("DSE expansion belongs to an unknown plan")
        self._dse_instances_by_template[event.template_id] = set(
            event.present_instances
        )
        for component_name in event.present_instances:
            self._dse_retirement_candidates.discard(
                (event.signature.metadata.id, component_name)
            )
        added_identities = set()
        for item in event.added_items:
            # Runtime DSE children, including cloned direct predicates, are
            # owned by the monitor which expanded them.  Their source type
            # still selects the collection adapter, but is not a routing key
            # for primary-to-monitor control commands.
            self._register_plan_owner(item.correlation_key, plan)
            self.work_items[item.correlation_key] = item
            self.correlation.register_work_item(
                event.signature, item, event.plan_generation
            )
            identity = (item.rule_id, item.component_name)
            added_identities.add(identity)
            self._dse_retirement_candidates.discard(identity)
        for key in event.removed_keys:
            item = self.work_items.pop(key, None)
            if item is None:
                continue
            if self._plan_by_work_key.get(key) is plan:
                self._plan_by_work_key.pop(key, None)
            identity = (item.rule_id, item.component_name)
            self._dse_retirement_candidates.add(identity)
            self.correlation.unregister_work_item(item)
            self.broken_rules.pop(key, None)
            self._forget_removed_work_state(key, item)
        reconciliation_identities = set(added_identities)
        reconciliation_identities.update(
            identity
            for identity in self.pending_dynamic_faults
            if identity[0] == event.signature.metadata.id
            and identity[1] in event.present_instances
        )
        for identity in reconciliation_identities:
            record = self.pending_dynamic_faults.get(identity)
            execution = self.correlation.executions.get(identity)
            if (
                record is None
                or execution is None
                or not execution.has_all_events
            ):
                continue
            self.pending_dynamic_faults.pop(identity, None)
            self.uncertain_faults.discard(identity)
            self._start_reconciliation(
                execution, "runtime_expansion_fault_reconciliation"
            )
        candidates = {
            identity
            for identity in self._dse_retirement_candidates
            if identity[0] == event.signature.metadata.id
        }
        candidates.update(
            identity
            for identity in self.pending_dynamic_faults
            if identity[0] == event.signature.metadata.id
        )
        for identity in sorted(candidates):
            self._retire_absent_dse_fault(identity, event)
        if event.removed_keys:
            self._refresh_fault_source_staleness()

    def _forget_removed_work_state(self, key, item) -> None:
        """Drop per-key failure state without erasing live source siblings."""

        self._primary_processing_failures.pop(key, None)
        failed_keys = self._source_failure_keys.get(item.source_id)
        if failed_keys is not None:
            failed_keys.discard(key)
        suspended_keys = self._suspended_sources.get(item.source_id)
        if suspended_keys is not None:
            suspended_keys.discard(key)

        if failed_keys:
            current = dict(self.source_status.get(item.source_id, {}))
            current.update(self._source_impact(failed_keys))
            self.source_status[item.source_id] = current
            if not suspended_keys:
                self._suspended_sources.pop(item.source_id, None)
            return

        self._source_failure_keys.pop(item.source_id, None)
        self._suspended_sources.pop(item.source_id, None)
        self._source_unavailable_since.pop(item.source_id, None)
        has_source_sibling = any(
            sibling.source_id == item.source_id
            for sibling in self.work_items.values()
        )
        if not has_source_sibling or self.source_status.get(
            item.source_id, {}
        ).get("state") != "RECOVERED":
            self.source_status.pop(item.source_id, None)

    def _retire_absent_dse_fault(
        self,
        identity: Tuple[int, str],
        event: DSEExpansionEvent,
    ) -> bool:
        """Retain and clear a fault only after DSE proves its instance is gone."""

        rule_id, component_name = identity
        template_ids = self._dse_template_ids_by_rule.get(rule_id, set())
        if not template_ids or any(
            template_id not in self._dse_instances_by_template
            or component_name
            in self._dse_instances_by_template[template_id]
            for template_id in template_ids
        ):
            return False

        # Monitor children remain registered while any key is collecting,
        # queued, held, or still owned by another template.  Waiting for the
        # complete expanded identity to disappear prevents a partial removal
        # from clearing a live fault.
        removed_keys = frozenset(event.removed_keys)
        if any(
            key not in removed_keys
            and item.rule_id == rule_id
            and item.component_name == component_name
            for plan in self.plans.values()
            for key, item in plan.expanded_item_snapshot().items()
        ):
            return False
        if identity in self.pending or identity in self.reconciliation:
            return False

        record = self.faults.get(identity)
        static_keys = tuple(
            key
            for key, item in self.work_items.items()
            if item.rule_id == rule_id
            and item.component_name == component_name
            and item.dse_binding is None
        )
        if static_keys:
            # The component still has independently materialized direct work.
            # Clear only the removed DSE event history and promptly re-evaluate
            # the remaining expression instead of forcing the whole component
            # inactive (for example, a DSE OR direct-Redis rule).
            self.pending_dynamic_faults.pop(identity, None)
            self._dse_retirement_candidates.discard(identity)
            if record is not None and record.status == "ACTIVE":
                self.uncertain_faults.add(identity)
                self._start_reconciliation(
                    self.correlation.executions[identity],
                    "dse_removal_remaining_work_reconciliation",
                )
            return False

        self.correlation.retire(rule_id, component_name)
        symptom = (
            record.symptom
            if record is not None
            else event.signature.metadata.symptom
        )
        winner = self.arbiter.retire(rule_id, component_name, symptom)
        self.pending_dynamic_faults.pop(identity, None)
        self.next_active_recheck.pop(identity, None)
        self.uncertain_faults.discard(identity)
        self._dse_retirement_candidates.discard(identity)
        if record is None:
            return True

        reason = bound_diagnostic(
            "DSE discovery no longer reports instance '{}'".format(
                component_name
            ),
            512,
        )
        record.status = "INACTIVE"
        record.reason = reason
        record.last_detection_time = event.observed_at
        record.inactive_deadline = (
            event.observed_at + self.config.inactive_fault_retention_period
        )
        record.repair_actions = ()
        record.stale_source = False

        fault_key = (component_name, record.symptom)
        owner = self.published_by_key.get(fault_key)
        if owner is None:
            self.published_by_key[fault_key] = rule_id
            owner = rule_id
        if owner == rule_id and winner is not None:
            alternate_identity = (
                winner.signature.metadata.id,
                component_name,
            )
            alternate = self.faults.get(alternate_identity)
            if alternate is not None and alternate.status == "ACTIVE":
                alternate.origin_time = record.origin_time
                alternate.occurrences = record.occurrences
                self.published_by_key[fault_key] = alternate.rule_id
                self._publish_fault_record(
                    alternate,
                    refresh_remote_window=True,
                )
            else:
                self._publish_fault_record(record)
        elif owner == rule_id:
            self._publish_fault_record(record)

        return True

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
        self.broken_rules[event.correlation_key] = self._broken_rule_payload(
            event,
            None,
            "primary_processing_error: {}".format(error),
            count,
            "BROKEN" if fatal else "DEGRADED",
            self.wall_clock(),
        )
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
        quarantine_reason = ""
        pending_reason = ""
        hold_deadline: Optional[float] = None
        complete: Any = None
        owned: Optional[Union[Reconciliation, PendingFault]] = (
            self.reconciliation.get(identity)
        )
        if owned is not None:
            quarantine_reason = "reconciliation_evidence_quarantined"
            pending_reason = "bootstrap_reconciliation_pending"
            hold_deadline = None
            complete = self._complete_reconciliation
        else:
            owned = self.pending.get(identity)
            if owned is not None:
                quarantine_reason = "local_action_evidence_quarantined"
                pending_reason = "post_action_recheck_pending"
                hold_deadline = owned.hold_deadline
                complete = self._complete_pending
        if owned is not None:
            if event.from_recheck:
                self._process_owned_recheck_evidence(
                    event,
                    identity,
                    owned,
                    pending_reason=pending_reason,
                    hold_deadline=hold_deadline,
                    complete=complete,
                )
            else:
                self._hold_owned_evidence(
                    event,
                    quarantine_reason,
                    hold_deadline=(
                        hold_deadline
                        if hold_deadline is not None
                        else self.clock() + self.config.fault_evidence_ack_timeout
                    ),
                )
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
                future: Future[ActionSequenceResult] = Future()
                future.dldd_worker_id = ""  # type: ignore[attr-defined]
                future.set_result(
                    ActionSequenceResult(
                        "",
                        "EXECUTION_ERROR",
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

    def _hold_owned_evidence(
        self,
        event: FaultEvidenceEvent,
        reason: str,
        hold_deadline: float,
    ) -> None:
        self._command_event(
            event,
            MonitorWorkState.HELD_BY_PRIMARY,
            reason,
            hold_deadline=hold_deadline,
        )

    def _process_owned_recheck_evidence(
        self,
        event: FaultEvidenceEvent,
        identity: Tuple[int, str],
        state: Any,
        *,
        pending_reason: str,
        hold_deadline: Optional[float],
        complete,
    ) -> None:
        """Apply one recheck result to a pending or reconciliation record."""

        result_type = event.result.result
        if result_type == EvaluationResultType.SOURCE_RECOVERED:
            self._process_runtime_status(event, release=False)
            state.recheck_deadline = (
                self.clock() + self.config.fault_evidence_ack_timeout
            )
            event_hold_deadline = hold_deadline
            if event_hold_deadline is None:
                event_hold_deadline = (
                    self.clock() + self.config.fault_evidence_ack_timeout
                )
            self._command_event(
                event,
                MonitorWorkState.RECHECK_REQUESTED,
                "source_recovered_recheck_required",
                hold_deadline=event_hold_deadline,
            )
            return

        decisive = result_type in _DECISIVE_RECHECK_RESULTS
        if not decisive:
            self._process_runtime_status(event, release=False)
        decision = self.correlation.consume(event)
        state.outstanding_rechecks.discard(event.correlation_key)
        if decision is not None:
            state.last_decision = decision
        if not decisive:
            state.recheck_failed = True
        event_hold_deadline = hold_deadline
        if event_hold_deadline is None:
            event_hold_deadline = (
                self.clock() + self.config.fault_evidence_ack_timeout
            )
        self._hold_owned_evidence(
            event,
            pending_reason,
            hold_deadline=event_hold_deadline,
        )
        if not state.outstanding_rechecks:
            complete(identity, state)

    def _source_impact(self, keys) -> Mapping[str, Any]:
        """Project failed work keys into the shared source-status fields."""

        return {
            "affected_rules": sorted(
                {
                    self.work_items[key].rule_id
                    for key in keys
                    if key in self.work_items
                }
            ),
            "stale_faults": self._stale_fault_keys(keys),
        }

    def _broken_rule_payload(
        self,
        event: FaultEvidenceEvent,
        runtime_status: Any,
        reason: str,
        failure_count: int,
        state: str,
        attempted_at: float,
    ) -> Mapping[str, Any]:
        """Build the canonical per-work-key runtime failure record."""

        item = self.work_items.get(event.correlation_key)
        rule_name = (
            runtime_status.rule_name
            if runtime_status is not None
            else item.rule_name if item is not None else str(event.signature_id)
        )
        return {
            "rule": rule_name,
            "version": item.rule_version if item is not None else "",
            "rule_id": event.signature_id,
            "correlation_key": event.correlation_key,
            "reason": reason,
            "failure_count": failure_count,
            "state": state,
            "last_attempt": attempted_at,
        }

    def _process_runtime_status(
        self, event: FaultEvidenceEvent, release: bool = True
    ) -> None:
        result = event.result
        status = event.runtime_status
        failure_count = status.failure_count if status is not None else 1
        last_success = status.last_success_timestamp if status is not None else None
        now = self.wall_clock()
        if result.result == EvaluationResultType.EVALUATION_ERROR:
            fatal = (
                not result.retryable
                or failure_count > self.config.individual_max_failure_threshold
            )
            self.broken_rules[event.correlation_key] = self._broken_rule_payload(
                event,
                status,
                "{}: {}".format(result.error_category.lower(), result.error),
                failure_count,
                "BROKEN" if fatal else "DEGRADED",
                now,
            )
            if release:
                self._command_event(
                    event,
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
                current.update(self._source_impact(failed_keys))
                self.source_status[event.source_id] = current
            else:
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
                "last_success": last_success,
                "failure_count": failure_count,
                **self._source_impact(failed_keys),
            }
            self._refresh_fault_source_staleness()
            if release:
                self._command_event(
                    event,
                    MonitorWorkState.SUSPENDED,
                    "expected source maintenance",
                )
            return
        within_grace = (
            now - first_failure < self.config.source_unavailable_grace_period
        )
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
            "last_success": last_success,
            "failure_count": failure_count,
            **self._source_impact(failed_keys),
        }
        if not within_grace or not result.retryable:
            self.broken_rules[event.correlation_key] = self._broken_rule_payload(
                event,
                status,
                "{}: {}".format(result.error_category.lower(), result.error),
                failure_count,
                state,
                now,
            )
        self._refresh_fault_source_staleness()
        if not release:
            return
        if fatal:
            self._command_event(
                event,
                MonitorWorkState.BROKEN,
                "fatal or threshold-exceeded rule failure",
            )
        else:
            self._command_event(
                event,
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
                execution.work_keys
            ):
                stale.append(fault.redis_key)
        return stale

    def _source_outage_is_expected(self, key: str) -> bool:
        if self.source_lifecycle_probe is None:
            return False
        try:
            return bool(self.source_lifecycle_probe(self.work_items[key]))
        except Exception as error:
            LOGGER.warning(
                "source lifecycle probe failed for %s; treating source as "
                "unavailable: %s",
                self.work_items[key].component_name,
                error,
            )
            return False

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
                self._source_outage_is_expected(key)
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
                    MonitorWorkState.DEGRADED,
                    "expected source maintenance ended",
                )

    def _start_pending(
        self, decision: CorrelationDecision, future: Optional[Future] = None
    ) -> None:
        execution = decision.execution
        identity = (execution.signature.metadata.id, execution.component_name)
        local = execution.signature.actions.repair_actions.local_actions
        actions = tuple(item.as_runtime_payload() for item in local.action_list)
        if future is None:
            runner = self.action_runner
            if runner is None:
                raise RuntimeError("action runner unavailable")
            future = runner.submit(
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
        for key in execution.work_keys:
            self._command_key(
                key,
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
        record = self._new_fault_record(
            execution,
            status="CANDIDATE",
            observed_at=decision.event.event_timestamp,
            occurrences=occurrences,
            events=decision.event_snapshots,
        )
        record.local_action_state = "RUNNING"
        record.local_action_details = details
        record.action_suppressed = True
        self.faults[identity] = record

    def _new_fault_record(
        self,
        execution: SignatureExecution,
        *,
        status: str,
        observed_at: float,
        occurrences: int = 1,
        events=(),
    ) -> FaultRecord:
        """Build the rule-owned fields shared by candidate and fault records."""

        metadata = execution.signature.metadata
        return FaultRecord(
            rule_id=metadata.id,
            rule_name=metadata.name,
            rule_version=metadata.version,
            schema_version=execution.signature.schema_version,
            active_rules_checksum=self.active_rules_checksum,
            component_type=metadata.component,
            component_name=execution.component_name,
            symptom=metadata.symptom,
            severity=metadata.severity,
            priority=metadata.priority,
            error_type=metadata.error_type,
            description=metadata.description,
            status=status,
            origin_time=observed_at,
            last_detection_time=observed_at,
            occurrences=occurrences,
            events=tuple(events),
            inactive_deadline=(
                observed_at + self.config.inactive_fault_retention_period
                if status == "INACTIVE"
                else None
            ),
        )

    def _request_rechecks(
        self,
        keys,
        reason: str,
        hold_deadline: float,
    ) -> None:
        for key in keys:
            self._command_key(
                key,
                MonitorWorkState.RECHECK_REQUESTED,
                reason,
                hold_deadline=hold_deadline,
            )

    def _retry_or_timeout_rechecks(
        self,
        identity: Tuple[int, str],
        state: Any,
        now: float,
        *,
        retry_reason: str,
        timeout_reason: str,
        complete,
    ) -> None:
        """Retry an owned recheck once, then complete it conservatively."""

        if (
            not state.outstanding_rechecks
            or state.recheck_deadline is None
            or now < state.recheck_deadline
        ):
            return
        if (
            state.recheck_attempts < _MAX_RECHECK_ATTEMPTS
            and now < state.hold_deadline
        ):
            state.recheck_attempts += 1
            state.recheck_deadline = (
                now + self.config.fault_evidence_ack_timeout
            )
            self._request_rechecks(
                state.outstanding_rechecks,
                retry_reason,
                state.hold_deadline,
            )
            return

        state.recheck_failed = True
        self.service_diagnostics.append(
            {
                "reason": timeout_reason,
                "rule_id": identity[0],
                "component": identity[1],
                "state": "ACTIVE",
                "observed_at": self.wall_clock(),
            }
        )
        complete(identity, state)

    def tick(self) -> None:
        self._apply_queued_config_updates()
        now = self.clock()
        self._retry_dirty_faults()
        self._recover_expected_source_suspensions()
        for identity, pending in list(self.pending.items()):
            action_finished = False
            action_error = None
            action_deadline_expired = False
            if pending.phase == "ACTIONS" and pending.future.done():
                action_finished = True
                try:
                    pending.action_result = pending.future.result()
                except Exception as error:
                    action_error = str(error)
            elif pending.phase == "ACTIONS" and now >= pending.action_deadline:
                action_finished = True
                action_deadline_expired = True
                action_error = (
                    "action worker did not complete before the action deadline"
                )

            if action_error is not None:
                now_wall = self.wall_clock()
                pending.action_result = ActionSequenceResult(
                    getattr(pending.future, "dldd_worker_id", ""),
                    "TIMED_OUT" if action_deadline_expired else "EXECUTION_ERROR",
                    pending.first_decision.event.event_timestamp,
                    now_wall,
                    (),
                    action_error,
                )
            if action_deadline_expired and pending.action_result is not None:
                self.service_diagnostics.append(
                    {
                        "reason": "local_action_deadline_expired",
                        "rule_id": identity[0],
                        "component": identity[1],
                        "observed_at": pending.action_result.completed_at,
                    }
                )
            if action_finished:
                pending.artifact = self._request_artifact(
                    pending.execution, pending.action_result
                )
                wait_period = (
                    pending.execution.signature.actions.repair_actions.local_actions.wait_period
                )
                pending.wait_until = now + wait_period
                pending.phase = "WAITING_FOR_RECHECK"
            if (
                pending.phase == "WAITING_FOR_RECHECK"
                and pending.wait_until is not None
                and now >= pending.wait_until
            ):
                pending.outstanding_rechecks = set(
                    pending.execution.work_keys
                )
                pending.phase = "RECHECKING"
                pending.recheck_attempts = 1
                pending.recheck_deadline = (
                    now + self.config.fault_evidence_ack_timeout
                )
                self._request_rechecks(
                    pending.outstanding_rechecks,
                    "post_action_recheck",
                    pending.hold_deadline,
                )
            if pending.phase == "RECHECKING":
                self._retry_or_timeout_rechecks(
                    identity,
                    pending,
                    now,
                    retry_reason="post_action_recheck_retry",
                    timeout_reason="post_action_recheck_timed_out",
                    complete=self._complete_pending,
                )

        for identity, reconciliation in list(self.reconciliation.items()):
            self._retry_or_timeout_rechecks(
                identity,
                reconciliation,
                now,
                retry_reason="{}_retry".format(reconciliation.reason),
                timeout_reason="{}_timed_out".format(reconciliation.reason),
                complete=self._complete_reconciliation,
            )

        for identity, deadline in list(self.next_active_recheck.items()):
            if now < deadline or identity in self.pending or identity in self.reconciliation:
                continue
            record = self.faults.get(identity)
            execution = self.correlation.executions.get(identity)
            if record is None or execution is None or record.status != "ACTIVE":
                self.next_active_recheck.pop(identity, None)
                continue
            self._start_reconciliation(execution, "active_fault_periodic_recheck")

    def source_status_snapshot(self) -> Tuple[Mapping[str, Any], ...]:
        """Return source state after expiring primary-owned recovery rows."""

        recovered_cutoff = self.wall_clock() - 30
        for source_id, source_record in tuple(self.source_status.items()):
            if (
                source_record.get("state") == "RECOVERED"
                and source_record.get("since", 0) < recovered_cutoff
            ):
                self.source_status.pop(source_id, None)
        return tuple(self.source_status.values())

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
        action_state = (
            pending.action_result.state
            if pending.action_result
            else "EXECUTION_ERROR"
        )
        actions_taken = (
            tuple(item.as_payload() for item in pending.action_result.actions)
            if pending.action_result
            else ()
        )
        action_details = {
            "state": action_state,
            "rule_instance_id": make_rule_instance_id(*identity),
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
        if action_state != "COMPLETED":
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
        uncertain = pending.recheck_failed
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
        for key in pending.execution.work_keys:
            self._release_key(key, "post_action_lifecycle_complete")
        self.pending.pop(identity, None)

    def reconcile_existing_faults(self) -> None:
        """Recheck current-generation active records before normal publication."""

        for payload in self.telemetry.read_faults():
            if not is_dldd_fault_payload(payload):
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
            dynamic_signature = self.dynamic_signatures.get(record.rule_id)
            current = (
                execution is not None
                and execution.has_all_events
                and record.schema_version == execution.signature.schema_version
                and record.active_rules_checksum == self.active_rules_checksum
                and execution.signature.metadata.symptom == record.symptom
            )
            pending_dynamic = (
                (execution is None or not execution.has_all_events)
                and dynamic_signature is not None
                and record.schema_version == dynamic_signature.schema_version
                and record.active_rules_checksum == self.active_rules_checksum
                and dynamic_signature.metadata.symptom == record.symptom
            )
            if record.status != "ACTIVE":
                # Retained inactive rows own occurrence history even if a new
                # generation replaces the rule that originally produced them.
                record.inactive_deadline = (
                    record.last_detection_time
                    + self.config.inactive_fault_retention_period
                )
                self.faults[identity] = record
                self.published_by_key[
                    (record.component_name, record.symptom)
                ] = record.rule_id
                if pending_dynamic:
                    # A current-generation dynamic component may have vanished
                    # while DLDD was stopped.  Keep the retained history row,
                    # but let the first successful current inventory either
                    # confirm the instance or refresh the row with the explicit
                    # DSE-removal reason and normal inactive TTL.
                    self._dse_retirement_candidates.add(identity)
                continue
            if pending_dynamic:
                # Runtime-expanded instances do not exist when startup fault
                # reconciliation first runs. Preserve current-generation fault
                # ownership until discovery recreates the matching execution;
                # retiring it here would create a false clear on every restart.
                self.faults[identity] = record
                self.published_by_key[
                    (record.component_name, record.symptom)
                ] = record.rule_id
                self.pending_dynamic_faults[identity] = record
                self.uncertain_faults.add(identity)
                continue
            if not current:
                record.status = "INACTIVE"
                record.repair_actions = ()
                record.last_detection_time = self.wall_clock()
                record.inactive_deadline = (
                    record.last_detection_time
                    + self.config.inactive_fault_retention_period
                )
                record.reason = bound_diagnostic(
                    "stale rule/source after DLDD restart", 512
                )
                self.faults[identity] = record
                self.published_by_key[
                    (record.component_name, record.symptom)
                ] = record.rule_id
                self._publish_fault_record(record)
                continue
            self.faults[identity] = record
            self.published_by_key[(record.component_name, record.symptom)] = record.rule_id
            if execution is None:
                continue
            self._start_reconciliation(execution, "bootstrap_fault_reconciliation")

    def _start_reconciliation(
        self, execution: SignatureExecution, reason: str
    ) -> None:
        identity = (execution.signature.metadata.id, execution.component_name)
        keys = set(execution.work_keys)
        now = self.clock()
        deadline = now + self.config.fault_evidence_ack_timeout
        hold_deadline = (
            now + (2 * self.config.fault_evidence_ack_timeout) + 30
        )
        self.reconciliation[identity] = Reconciliation(
            execution,
            keys,
            reason=reason,
            hold_deadline=hold_deadline,
            recheck_deadline=deadline,
        )
        self._request_rechecks(keys, reason, hold_deadline)

    def _complete_reconciliation(self, identity, reconciliation) -> None:
        decision = reconciliation.last_decision
        record = self.faults[identity]
        state_changed = False
        uncertain = reconciliation.recheck_failed
        if uncertain:
            self.uncertain_faults.add(identity)
        else:
            self.uncertain_faults.discard(identity)
        if decision is None:
            active = True
            self.correlation.set_active(identity[0], identity[1], True)
            self.arbiter.restore_active(
                reconciliation.execution,
                record.origin_time,
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
                self._publish_fault_record(record)
        self._refresh_fault_source_staleness()
        for key in reconciliation.execution.work_keys:
            self._release_key(key, "{}_complete".format(reconciliation.reason))
        self.reconciliation.pop(identity, None)

    @staticmethod
    def _fault_from_payload(payload: Mapping[str, Any]) -> FaultRecord:
        component_type = str(payload.get("component_type", "")).strip()
        component_name = str(payload.get("component_name", "")).strip()
        if not component_type or not component_name:
            raise ValueError(
                "component_type and component_name must be non-empty"
            )
        local = payload.get("local_action_state") or {}
        repairs = payload.get("repair_actions") or []
        return FaultRecord(
            rule_id=int(payload.get("rule_id", 0)),
            rule_name=str(payload.get("rule", "")),
            rule_version=str(payload.get("rule_version", "")),
            schema_version=str(payload.get("schema_version", "")),
            active_rules_checksum=str(payload.get("active_rules_checksum", "")),
            component_type=component_type,
            component_name=component_name,
            symptom=str(payload.get("symptom", "")),
            severity=str(payload.get("severity", "UNKNOWN")),
            priority=int(payload.get("priority", 5)),
            error_type=str(payload.get("error_type", "")),
            description=str(payload.get("description", "")),
            reason=str(payload.get("reason", "")),
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
            serial_number=str(payload.get("component_serial_number", "")),
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
            existing = self._new_fault_record(
                execution,
                status=status,
                observed_at=now,
            )
            self.faults[identity] = existing
        elif existing.status == "INACTIVE" and status == "ACTIVE":
            existing.occurrences += 1
            existing.origin_time = now
        # A stale record can become active again under a newly selected rules
        # generation. Refresh all rule-owned metadata so the new active fault
        # does not retain the previous checksum or stale-source description.
        existing.rule_name = metadata.name
        existing.rule_version = metadata.version
        existing.schema_version = execution.signature.schema_version
        existing.active_rules_checksum = self.active_rules_checksum
        existing.component_type = metadata.component
        existing.symptom = metadata.symptom
        existing.severity = metadata.severity
        existing.priority = metadata.priority
        existing.error_type = metadata.error_type
        existing.description = metadata.description
        existing.reason = ""
        preserve_action_history = (
            status == "INACTIVE"
            and existing.local_action_state not in ("", "IDLE")
            and local_action_state == "IDLE"
            and not actions_taken
        )
        existing.status = status
        existing.inactive_deadline = (
            now + self.config.inactive_fault_retention_period
            if status == "INACTIVE"
            else None
        )
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
        existing.stale_source = (
            stale_source or self._execution_has_failed_source(execution)
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
                    alternate.remote_action_time_window = alternate_remote.time_window
                    self._publish_fault_record(alternate)
                    return
            # The retained inactive row remains the owner of the Redis key so
            # a later competing signature can inherit occurrence history.
            self.published_by_key[fault_key] = metadata.id

        self._publish_fault_record(existing)

    def _refresh_fault_source_staleness(self) -> None:
        failed_keys = self._failed_correlation_keys()
        for identity, record in self.faults.items():
            if record.status != "ACTIVE":
                continue
            execution = self.correlation.executions.get(identity)
            stale = bool(
                identity in self.uncertain_faults
                or (
                    execution is not None
                    and failed_keys.intersection(execution.work_keys)
                )
            )
            if record.stale_source == stale:
                continue
            record.stale_source = stale
            self._publish_fault_record(record, refresh_remote_window=True)

    def _failed_correlation_keys(self) -> Set[str]:
        return {
            key
            for source_keys in self._source_failure_keys.values()
            for key in source_keys
        }

    def _execution_has_failed_source(self, execution: SignatureExecution) -> bool:
        return bool(
            self._failed_correlation_keys().intersection(
                execution.work_keys
            )
        )

    def _publish_fault_record(
        self,
        record: FaultRecord,
        *,
        refresh_remote_window: bool = False,
    ) -> bool:
        identity = (record.rule_id, record.component_name)
        owner = self.published_by_key.get(
            (record.component_name, record.symptom)
        )
        if owner is not None and owner != record.rule_id:
            # Suppressed signatures retain internal/action/artifact state but
            # must never overwrite the one component/symptom Redis row owned
            # by the arbiter winner.
            self.dirty_faults.discard(identity)
            return True
        if refresh_remote_window:
            execution = self.correlation.executions.get(identity)
            if execution is not None:
                record.remote_action_time_window = (
                    execution.signature.actions.repair_actions.remote_actions.time_window
                )
        published = self.telemetry.publish_fault(
            record,
            remote_action_time_window=record.remote_action_time_window,
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
            self._publish_fault_record(record, refresh_remote_window=True)

    def _request_artifact(
        self,
        execution: SignatureExecution,
        action_result: Optional[ActionSequenceResult] = None,
    ):
        collection = execution.signature.actions.log_collection
        if collection is None or self.artifact_client is None:
            return None
        try:
            requested_at = self.wall_clock()
            metadata = {
                "rule": execution.signature.metadata.name,
                "rule_id": execution.signature.metadata.id,
                "timestamp": requested_at,
                "component_info": {
                    "component": execution.signature.metadata.component,
                    "name": execution.component_name,
                },
                "symptom": execution.signature.metadata.symptom,
            }
            if action_result is not None:
                outputs = tuple(
                    output
                    for index, action in enumerate(action_result.actions)
                    for output in (action.as_artifact_payload(index),)
                    if output is not None
                )
                if outputs:
                    metadata["action_outputs"] = outputs
            request = self.artifact_client.request(
                floor_timestamp_fields(metadata),
                collection.logs,
                tuple(item.as_runtime_payload() for item in collection.queries),
            )
            return request.as_payload()
        except Exception as error:
            return floor_timestamp_fields(
                {
                    "requested_at": self.wall_clock(),
                    "request_error": str(error),
                }
            )

    def _resume(self, event: FaultEvidenceEvent, reason: str) -> None:
        self._command_event(
            event,
            MonitorWorkState.READY,
            reason,
        )

    def _release_key(self, key: str, reason: str) -> None:
        broken = self.broken_rules.get(key)
        if broken is not None and broken.get("state") == "BROKEN":
            self._command_key(
                key,
                MonitorWorkState.BROKEN,
                reason,
            )
            return
        item = self.work_items[key]
        source_state = self.source_status.get(item.source_id, {}).get("state")
        if source_state == "SUSPENDED":
            self._command_key(
                key,
                MonitorWorkState.SUSPENDED,
                reason,
            )
            return
        unavailable = source_state == "UNAVAILABLE"
        self._command_key(
            key,
            MonitorWorkState.DEGRADED if unavailable else MonitorWorkState.READY,
            reason,
        )

    def _command_event(
        self,
        event: FaultEvidenceEvent,
        target: MonitorWorkState,
        reason: str,
        recheck_not_before: Optional[float] = None,
        hold_deadline: Optional[float] = None,
    ) -> None:
        self._command_key(
            event.correlation_key,
            target,
            reason,
            evidence=event,
            recheck_not_before=recheck_not_before,
            hold_deadline=hold_deadline,
        )

    def _command_key(
        self,
        key: str,
        target: MonitorWorkState,
        reason: str,
        evidence: Optional[FaultEvidenceEvent] = None,
        recheck_not_before: Optional[float] = None,
        hold_deadline: Optional[float] = None,
    ) -> None:
        item = self.work_items[key]
        plan = self._plan_by_work_key.get(key)
        if plan is None:
            # Preserve support for externally assembled plans and focused test
            # doubles. Production static and expanded work is registered with
            # an explicit owner above.
            monitor_type = monitor_type_for_source(item.source_type)
            plan = self.plans[monitor_type]
        plan.control_queue.put(
            MonitorControlCommand(
                command_id=str(uuid.uuid4()),
                monitor_id=plan.monitor_id,
                plan_generation=plan.plan_generation,
                correlation_key=key,
                command=_COMMAND_BY_TARGET[target],
                target_state=target,
                reason=reason,
                expected_work_state_generation=(
                    evidence.work_state_generation if evidence is not None else None
                ),
                evidence_sequence=(
                    evidence.sequence if evidence is not None else None
                ),
                recheck_not_before=recheck_not_before,
                hold_deadline=hold_deadline,
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
