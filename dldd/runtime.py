"""In-process DLDD runtime contracts.

These objects are deliberately plain dataclasses.  They cross thread queues but
are never persisted; Redis and JSON serialization happens only at the service
boundary.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from queue import Queue
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Set, Tuple
from urllib.parse import quote

from .models import ValueConfig


class MonitorCommandType(str, Enum):
    RESUME = "RESUME"
    HOLD = "HOLD"
    RECHECK_ONCE = "RECHECK_ONCE"
    SUSPEND = "SUSPEND"


class MonitorWorkState(str, Enum):
    READY = "READY"
    COLLECTING = "COLLECTING"
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
    sampling_interval: float = 60.0
    sampling_interval_is_explicit: bool = False
    async_collection: bool = False
    dse_context: Any = None
    dse_binding: Any = None
    dse_source_handle: Any = None
    dse_evaluation_handle: Any = None

    def __post_init__(self) -> None:
        interval = float(self.sampling_interval)
        if not math.isfinite(interval) or not 1 <= interval <= 0xFFFFFFFF:
            raise ValueError(
                "sampling_interval must be between 1 and 4294967295 seconds"
            )
        object.__setattr__(self, "sampling_interval", interval)
        object.__setattr__(
            self,
            "sampling_interval_is_explicit",
            bool(self.sampling_interval_is_explicit),
        )
        object.__setattr__(self, "async_collection", bool(self.async_collection))
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "evaluation", MappingProxyType(dict(self.evaluation)))


@dataclass(frozen=True)
class DSEWorkTemplate:
    template_id: str
    item: MonitorWorkItem
    signature: Any
    source_handle: Any
    evaluation_handle: Any = None
    common_items: Tuple[MonitorWorkItem, ...] = ()
    static_work_keys: Tuple[str, ...] = ()


@dataclass
class DSEExpansionState:
    phase: str = "BOOTSTRAP"
    bootstrap_scans_completed: int = 0
    warmup_cycles_completed: int = 0
    cycle_id: int = 0
    binding_fingerprint: Tuple[Tuple[str, str], ...] = ()
    child_keys: Set[str] = field(default_factory=set)
    pending_cycle_keys: Set[str] = field(default_factory=set)
    next_expansion_due: Optional[float] = None
    last_expansion_timestamp: Optional[float] = None
    last_complete_cycle_timestamp: Optional[float] = None
    last_error: str = ""
    authoritative: bool = False


@dataclass(frozen=True)
class DSEExpansionEvent:
    monitor_id: str
    plan_generation: str
    template_id: str
    signature: Any
    added_items: Tuple[MonitorWorkItem, ...] = ()
    removed_keys: Tuple[str, ...] = ()
    present_instances: Tuple[str, ...] = ()
    phase: str = "BOOTSTRAP"
    authoritative: bool = False
    observed_at: float = field(default_factory=time.time)


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
    last_attempt_timestamp: Optional[float] = None
    last_success_timestamp: Optional[float] = None
    consecutive_failure_count: int = 0
    recovery_success_count: int = 0
    source_status: SourceAvailability = SourceAvailability.AVAILABLE
    next_sample_due: Optional[float] = None


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
    interval_update_queue: Queue = field(default_factory=Queue)
    templates_by_key: Mapping[str, DSEWorkTemplate] = field(
        default_factory=lambda: MappingProxyType({})
    )
    expansion_state_by_key: Dict[str, DSEExpansionState] = field(
        default_factory=dict
    )
    expanded_items_by_key: Dict[str, MonitorWorkItem] = field(
        default_factory=dict
    )
    polling_intervals: Mapping[str, float] = field(default_factory=dict)
    _structure_lock: Any = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        defaults = self.polling_intervals or {
            source_group: self.polling_interval
            for source_group in ("redis", "file", "common")
        }
        self.polling_intervals = MappingProxyType(
            self.validated_polling_intervals(defaults)
        )
        self.polling_interval = self.polling_intervals[self.monitor_type]
        # Work assignments are immutable for the lifetime of a plan. An
        # inherited item's effective interval is selected from the atomic
        # source-group defaults; updates never replace work-item objects behind
        # other consumers.
        self.items_by_key = MappingProxyType(dict(self.items_by_key))
        self.templates_by_key = MappingProxyType(dict(self.templates_by_key))
        for key in self.templates_by_key:
            self.expansion_state_by_key.setdefault(key, DSEExpansionState())
        missing = set(self.items_by_key) - set(self.state_by_key)
        for key in missing:
            self.state_by_key[key] = MonitorWorkStateRecord()

    def item(self, key: str) -> Optional[MonitorWorkItem]:
        with self._structure_lock:
            return self.items_by_key.get(key) or self.expanded_items_by_key.get(key)

    def item_snapshot(self) -> Dict[str, MonitorWorkItem]:
        with self._structure_lock:
            items = dict(self.items_by_key)
            items.update(self.expanded_items_by_key)
            return items

    def runtime_snapshot(
        self,
    ) -> Tuple[Dict[str, MonitorWorkItem], Dict[str, MonitorWorkStateRecord]]:
        """Atomically snapshot work identities and their state records.

        State-record fields remain monitor-owned and may advance after the
        snapshot.  The lock protects the structural add/remove boundary so
        readers never observe half of a runtime DSE child registration.
        """

        with self._structure_lock:
            items = dict(self.items_by_key)
            items.update(self.expanded_items_by_key)
            return items, dict(self.state_by_key)

    def expanded_item_snapshot(self) -> Dict[str, MonitorWorkItem]:
        """Return a stable view of monitor-owned runtime DSE children."""

        with self._structure_lock:
            return dict(self.expanded_items_by_key)

    def add_expanded_item(self, item: MonitorWorkItem) -> None:
        with self._structure_lock:
            self.expanded_items_by_key[item.correlation_key] = item
            self.state_by_key.setdefault(
                item.correlation_key, MonitorWorkStateRecord()
            )

    def remove_expanded_item(self, key: str) -> None:
        with self._structure_lock:
            self.expanded_items_by_key.pop(key, None)
            self.state_by_key.pop(key, None)

    @staticmethod
    def validated_polling_interval(interval: float) -> float:
        interval = float(interval)
        if not math.isfinite(interval) or not 1 <= interval <= 0xFFFFFFFF:
            raise ValueError(
                "polling interval must be between 1 and 4294967295 seconds"
            )
        return interval

    @classmethod
    def validated_polling_intervals(
        cls, intervals: Mapping[str, float]
    ) -> Dict[str, float]:
        """Validate one complete atomic source-default cadence snapshot."""

        if not isinstance(intervals, Mapping):
            raise TypeError("polling intervals must be a mapping")
        required = ("redis", "file", "common")
        missing = [key for key in required if key not in intervals]
        if missing:
            raise ValueError(
                "polling intervals are missing {}".format(", ".join(missing))
            )
        return {
            key: cls.validated_polling_interval(intervals[key])
            for key in required
        }

    def queue_polling_interval_update(
        self, intervals: Mapping[str, float]
    ) -> None:
        """Queue a complete default-cadence update for the owning monitor."""

        self.interval_update_queue.put_nowait(
            self.validated_polling_intervals(intervals)
        )


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
    reason: str = ""
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
