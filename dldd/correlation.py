"""Primary-thread signature correlation and fault arbitration."""

from __future__ import annotations

import time
from bisect import insort
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Mapping, Optional, Tuple

from .logic import evaluate_logic
from .models import ValueConfig
from .runtime import EvaluationResultType, FaultEvidenceEvent


_SEVERITY = {
    "CRITICAL": 0,
    "MAJOR": 1,
    "WARNING": 2,
    "MINOR": 3,
    "UNKNOWN": 4,
}


@dataclass(frozen=True)
class SignatureExecution:
    """One materialized rule instance and its ordered monitor work keys."""

    signature: Any
    component_name: str
    event_keys: Mapping[int, Tuple[str, ...]]
    plan_generation: str

    @property
    def work_keys(self) -> Tuple[str, ...]:
        """Flatten keys in event insertion order and per-event tuple order."""

        return tuple(
            key
            for event_keys in self.event_keys.values()
            for key in event_keys
        )


@dataclass(frozen=True)
class CorrelationDecision:
    execution: SignatureExecution
    active: bool
    changed: bool
    event_snapshots: Tuple[Mapping[str, Any], ...]
    event: FaultEvidenceEvent


@dataclass
class _EventState:
    matches: Deque[float]
    matching_keys: Dict[str, bool]
    key_timestamps: Dict[str, float]
    last_match: Optional[float] = None
    latest_timestamp: Optional[float] = None
    snapshot: Optional[Mapping[str, Any]] = None


class CorrelationEngine:
    """Correlate monitor evidence using event timestamps, never FIFO order."""

    def __init__(self, executions: Mapping[Tuple[int, str], SignatureExecution]) -> None:
        self.executions = dict(executions)
        self._events: Dict[Tuple[int, str, int], _EventState] = {}
        self._active: Dict[Tuple[int, str], bool] = defaultdict(bool)
        self.late_events_discarded = 0
        self.diagnostics = deque(maxlen=64)

    def register_work_item(self, signature, item, plan_generation: str) -> None:
        """Register one monitor-expanded key before its evidence is consumed."""

        identity = (item.rule_id, item.component_name)
        execution = self.executions.get(identity)
        event_keys = (
            {key: tuple(value) for key, value in execution.event_keys.items()}
            if execution is not None
            else {}
        )
        keys = list(event_keys.get(item.event_id, ()))
        if item.correlation_key not in keys:
            keys.append(item.correlation_key)
        event_keys[item.event_id] = tuple(sorted(keys))
        self.executions[identity] = SignatureExecution(
            signature=signature,
            component_name=item.component_name,
            event_keys=event_keys,
            plan_generation=plan_generation,
        )

    def unregister_work_item(self, item) -> None:
        identity = (item.rule_id, item.component_name)
        execution = self.executions.get(identity)
        if execution is None:
            return
        # Inventory removal invalidates this event's sampled truth.  Keeping
        # it would let a rediscovered instance combine new evidence with stale
        # pre-removal history.
        self._events.pop((item.rule_id, item.component_name, item.event_id), None)
        self._active.pop(identity, None)
        event_keys = {
            event_id: tuple(
                key
                for key in keys
                if key != item.correlation_key
            )
            for event_id, keys in execution.event_keys.items()
        }
        event_keys = {
            event_id: keys for event_id, keys in event_keys.items() if keys
        }
        if event_keys:
            self.executions[identity] = SignatureExecution(
                signature=execution.signature,
                component_name=execution.component_name,
                event_keys=event_keys,
                plan_generation=execution.plan_generation,
            )
        else:
            self.executions.pop(identity, None)

    def consume(self, event: FaultEvidenceEvent) -> Optional[CorrelationDecision]:
        execution = self.executions.get((event.signature_id, event.component_name))
        if execution is None:
            return None
        if event.event_id not in execution.event_keys:
            return None
        result_type = event.result.result
        if result_type not in (EvaluationResultType.MATCH, EvaluationResultType.NO_MATCH):
            return CorrelationDecision(
                execution, self._active[(event.signature_id, event.component_name)], False, (), event
            )

        event_definition = next(
            item
            for item in execution.signature.conditions.events
            if item.id == event.event_id
        )
        key = (event.signature_id, event.component_name, event.event_id)
        state = self._events.setdefault(key, _EventState(deque(), {}, {}))
        timestamp = event.event_timestamp
        allowed_lateness = max(
            event_definition.match_period,
            execution.signature.conditions.logic_lookback_time,
        )
        if (
            state.latest_timestamp is not None
            and timestamp < state.latest_timestamp - allowed_lateness
        ):
            self.late_events_discarded += 1
            self.diagnostics.append(
                {
                    "reason": "late_event_discarded",
                    "correlation_key": event.correlation_key,
                    "event_timestamp": timestamp,
                    "latest_timestamp": state.latest_timestamp,
                    "allowed_lateness": allowed_lateness,
                    "observed_at": time.time(),
                }
            )
            identity = (event.signature_id, event.component_name)
            return CorrelationDecision(
                execution, self._active[identity], False, (), event
            )
        state.latest_timestamp = max(
            timestamp, state.latest_timestamp or timestamp
        )
        key_timestamp = state.key_timestamps.get(event.correlation_key)
        if result_type == EvaluationResultType.MATCH:
            if key_timestamp is None or timestamp >= key_timestamp:
                insort(state.matches, timestamp)
                state.matching_keys[event.correlation_key] = True
                state.key_timestamps[event.correlation_key] = timestamp
                if state.last_match is None or timestamp >= state.last_match:
                    state.last_match = timestamp
                    state.snapshot = self._snapshot(event)
            elif state.matching_keys.get(event.correlation_key, False):
                insort(state.matches, timestamp)
        else:
            if key_timestamp is None or timestamp >= key_timestamp:
                state.matching_keys[event.correlation_key] = False
                state.key_timestamps[event.correlation_key] = timestamp
                if not any(state.matching_keys.values()):
                    state.matches.clear()
                    state.last_match = None

        evaluation_time = max(
            (
                item.latest_timestamp
                for item_key, item in self._events.items()
                if item_key[:2] == (event.signature_id, event.component_name)
                and item.latest_timestamp is not None
            ),
            default=timestamp,
        )
        event_truth = {}
        snapshots = []
        lookback = execution.signature.conditions.logic_lookback_time
        for definition in execution.signature.conditions.events:
            other = self._events.get(
                (event.signature_id, event.component_name, definition.id)
            )
            truth = self._event_truth(other, definition, evaluation_time)
            if truth and other is not None and other.last_match is not None:
                if lookback and evaluation_time - other.last_match > lookback:
                    truth = False
            event_truth[definition.id] = truth
            if truth and other is not None and other.snapshot is not None:
                snapshots.append(other.snapshot)

        active = evaluate_logic(execution.signature.conditions.logic_tree, event_truth)
        identity = (event.signature_id, event.component_name)
        changed = active != self._active[identity]
        self._active[identity] = active
        return CorrelationDecision(execution, active, changed, tuple(snapshots), event)

    def set_active(self, rule_id: int, component_name: str, active: bool) -> None:
        self._active[(rule_id, component_name)] = active

    def retire(self, rule_id: int, component_name: str) -> None:
        """Forget all correlation history for a removed runtime instance."""

        identity = (rule_id, component_name)
        self._active.pop(identity, None)
        for key in tuple(self._events):
            if key[:2] == identity:
                self._events.pop(key, None)

    @staticmethod
    def _event_truth(state, definition, now: float) -> bool:
        if state is None or not any(state.matching_keys.values()):
            return False
        if definition.match_period == 0:
            return definition.match_count == 1 and state.last_match is not None
        cutoff = now - definition.match_period
        while state.matches and state.matches[0] < cutoff:
            state.matches.popleft()
        return len(state.matches) >= definition.match_count

    @staticmethod
    def _snapshot(event: FaultEvidenceEvent) -> Mapping[str, Any]:
        result = event.result
        config = (
            result.value.config
            if result.value is not None
            else ValueConfig()
        )
        value_configs = config.as_payload()
        condition_config = result.condition_config
        condition_value_configs = condition_config.as_payload()
        return {
            "id": event.event_id,
            "value_read": CorrelationEngine._format_value(
                result.value.raw if result.value is not None else None, config
            ),
            "value_configs": value_configs,
            "condition": {
                "type": result.evaluator_type,
                "value": CorrelationEngine._format_value(
                    result.expected, condition_config
                ),
                "value_configs": condition_value_configs,
            },
        }

    @staticmethod
    def _format_value(value: Any, config: ValueConfig) -> Any:
        """Return the rule-described value in a JSON-safe representation."""

        if isinstance(value, bytes):
            value_type = str(config.type).lower()
            encoding = str(config.encoding)
            if encoding and encoding != "N/A":
                return value.decode(encoding, "replace")
            if value_type == "binary":
                return "0b" + "".join("{:08b}".format(byte) for byte in value)
            if value_type == "hex":
                return "0x" + value.hex()
            # A list retains every byte without inventing an encoding.
            return list(value)
        if isinstance(value, tuple):
            return [CorrelationEngine._format_value(item, config) for item in value]
        if isinstance(value, list):
            return [CorrelationEngine._format_value(item, config) for item in value]
        if isinstance(value, dict):
            return {
                str(key): CorrelationEngine._format_value(item, config)
                for key, item in value.items()
            }
        return value


class FaultArbiter:
    """Choose one published signature per component/symptom."""

    def __init__(self) -> None:
        self._active = {}
        self._detected_at = {}

    def restore_active(
        self, execution: SignatureExecution, detected_at: float
    ) -> None:
        """Restore one reconciled active signature without synthetic evidence."""

        metadata = execution.signature.metadata
        identity = (execution.component_name, metadata.symptom, metadata.id)
        self._active[identity] = execution
        self._detected_at.setdefault(identity, detected_at)

    def update(self, decision: CorrelationDecision) -> Optional[SignatureExecution]:
        metadata = decision.execution.signature.metadata
        identity = (
            decision.execution.component_name,
            metadata.symptom,
            metadata.id,
        )
        fault_key = identity[:2]
        if decision.active:
            self._active[identity] = decision.execution
            self._detected_at.setdefault(identity, decision.event.event_timestamp)
        else:
            self._active.pop(identity, None)
            self._detected_at.pop(identity, None)
        return self._winner(fault_key)

    def retire(
        self, rule_id: int, component_name: str, symptom: str
    ) -> Optional[SignatureExecution]:
        """Remove one execution whose runtime component no longer exists."""

        identity = (component_name, symptom, rule_id)
        self._active.pop(identity, None)
        self._detected_at.pop(identity, None)
        return self._winner((component_name, symptom))

    def _winner(self, fault_key) -> Optional[SignatureExecution]:
        candidates = [
            (key, execution)
            for key, execution in self._active.items()
            if key[:2] == fault_key
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                _SEVERITY.get(item[1].signature.metadata.severity, 99),
                item[1].signature.metadata.priority,
                self._detected_at[item[0]],
            )
        )
        return candidates[0][1]
