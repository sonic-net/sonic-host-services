"""In-process DLDD runtime contracts.

These objects are deliberately plain dataclasses.  They cross thread queues but
are never persisted; Redis and JSON serialization happens only at the service
boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from queue import Queue
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote


class MonitorCommandType(str, Enum):
    RESUME = "RESUME"
    HOLD = "HOLD"
    RECHECK_ONCE = "RECHECK_ONCE"
    SUSPEND = "SUSPEND"


class MonitorWorkState(str, Enum):
    READY = "READY"
    IN_FLIGHT = "IN_FLIGHT"
    HELD_BY_PRIMARY = "HELD_BY_PRIMARY"
    RECHECK_REQUESTED = "RECHECK_REQUESTED"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"
    SUSPENDED = "SUSPENDED"


class EvaluationResultType(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_RECOVERED = "SOURCE_RECOVERED"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    EVALUATION_ERROR = "EVALUATION_ERROR"


class SourceAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    SUSPENDED = "SUSPENDED"
    UNAVAILABLE = "UNAVAILABLE"
    RECOVERED = "RECOVERED"


@dataclass(frozen=True)
class ValueConfig:
    type: str = "N/A"
    unit: str = "N/A"
    scaling: Any = "N/A"
    encoding: str = "N/A"


@dataclass(frozen=True)
class CollectedValue:
    raw: Any
    normalized: Any
    config: ValueConfig = field(default_factory=ValueConfig)


@dataclass(frozen=True)
class EvaluationResult:
    result: EvaluationResultType
    value: Optional[CollectedValue] = None
    evaluator_type: str = ""
    operator: str = ""
    expected: Any = None
    condition_config: ValueConfig = field(default_factory=ValueConfig)
    source_status: SourceAvailability = SourceAvailability.AVAILABLE
    collection_started_at: float = 0.0
    completed_at: float = 0.0
    source_timestamp: Optional[float] = None
    error_category: str = ""
    error: str = ""
    retryable: bool = True


@dataclass(frozen=True)
class MonitorWorkItem:
    rule_id: int
    rule_name: str
    rule_version: str
    schema_version: str
    severity: str
    priority: int
    symptom: str
    error_type: str
    component_type: str
    component_name: str
    event_id: int
    correlation_key: str
    source_id: str
    source_type: str
    source: Mapping[str, Any]
    evaluation: Mapping[str, Any]
    match_count: int = 1
    match_period: int = 0
    value_config: ValueConfig = field(default_factory=ValueConfig)
    common_predicate: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "evaluation", MappingProxyType(dict(self.evaluation)))


@dataclass
class MonitorWorkStateRecord:
    state: MonitorWorkState = MonitorWorkState.READY
    work_state_generation: int = 0
    last_evidence_sequence: Optional[int] = None
    last_enqueue_timestamp: Optional[float] = None
    ack_deadline: Optional[float] = None
    hold_deadline: Optional[float] = None
    recheck_not_before: Optional[float] = None
    last_sample_state: Optional[str] = None
    last_success_timestamp: Optional[float] = None
    consecutive_failure_count: int = 0
    recovery_success_count: int = 0
    source_status: SourceAvailability = SourceAvailability.AVAILABLE


@dataclass(frozen=True)
class MonitorControlCommand:
    command_id: str
    monitor_id: str
    plan_generation: str
    correlation_key: str
    command: MonitorCommandType
    target_state: MonitorWorkState
    reason: str
    expected_work_state_generation: Optional[int] = None
    evidence_sequence: Optional[int] = None
    recheck_not_before: Optional[float] = None
    hold_deadline: Optional[float] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class MonitorExecutionPlan:
    monitor_id: str
    monitor_type: str
    polling_interval: float
    plan_generation: str
    items_by_key: Mapping[str, MonitorWorkItem]
    state_by_key: Dict[str, MonitorWorkStateRecord]
    control_queue: Queue

    def __post_init__(self) -> None:
        self.items_by_key = MappingProxyType(dict(self.items_by_key))
        missing = set(self.items_by_key) - set(self.state_by_key)
        for key in missing:
            self.state_by_key[key] = MonitorWorkStateRecord()


@dataclass(frozen=True)
class RuleRuntimeStatus:
    state: str
    rule_id: int
    rule_name: str
    event_id: int
    component_name: str
    source_id: str
    correlation_key: str
    reason: str = ""
    error_category: str = ""
    failure_count: int = 0
    last_success_timestamp: Optional[float] = None
    last_attempt_timestamp: Optional[float] = None
    retryable: bool = True


@dataclass(frozen=True)
class FaultEvidenceEvent:
    signature_id: int
    event_id: int
    component_name: str
    source_id: str
    correlation_key: str
    monitor_id: str
    plan_generation: str
    work_state_generation: int
    sequence: int
    event_timestamp: float
    enqueue_timestamp: float
    result: EvaluationResult
    from_recheck: bool = False
    runtime_status: Optional[RuleRuntimeStatus] = None


@dataclass
class FaultRecord:
    rule_id: int
    rule_name: str
    rule_version: str
    schema_version: str
    active_rules_checksum: str
    component_type: str
    component_name: str
    symptom: str
    severity: str
    priority: int
    error_type: str
    description: str = ""
    status: str = "ACTIVE"
    origin_time: float = field(default_factory=time.time)
    last_detection_time: float = field(default_factory=time.time)
    occurrences: int = 1
    events: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    repair_actions: Tuple[str, ...] = field(default_factory=tuple)
    remote_action_time_window: int = 0
    actions_taken: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    local_action_state: str = "IDLE"
    local_action_details: Mapping[str, Any] = field(default_factory=dict)
    action_suppressed: bool = False
    healthz_artifact: Optional[Mapping[str, Any]] = None
    serial_number: str = ""
    stale_source: bool = False
    inactive_deadline: Optional[float] = None

    @property
    def redis_key(self) -> str:
        return "FAULT_INFO|{}|{}".format(
            quote(self.component_name, safe=""), quote(self.symptom, safe="")
        )


def make_correlation_key(
    rule_id: int,
    event_id: int,
    component_name: str,
    symptom: str,
    source_id: str,
) -> str:
    """Return a stable, human-readable key without runtime sample data."""

    return "{}:{}:{}:{}:{}".format(
        rule_id, event_id, component_name, symptom, source_id
    )
