"""Explicit, non-persistent runtime qualification for installed rules."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping, Tuple

from .correlation import CorrelationEngine
from .planner import work_items_for_dse_expansion
from .runtime import (
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
)


@dataclass(frozen=True)
class E2EQualificationResult:
    event_results: Tuple[Mapping, ...]
    rule_results: Tuple[Mapping, ...]
    failed: bool


def _event_result(item, state, *, stage="execution", **details) -> Mapping:
    result = {
        "rule": item.rule_name,
        "rule_id": item.rule_id,
        "event_id": item.event_id,
        "component": item.component_name,
        "correlation_key": item.correlation_key,
        "stage": stage,
        "state": state,
    }
    result.update(details)
    return result


def qualify_e2e(
    bundle, adapters, invalid_rule_ids=()
) -> E2EQualificationResult:
    """Run one complete, non-remediating monitoring pass.

    This deliberately uses the live adapter ``collect()`` and correlation
    implementations.  It does not start monitor/orchestrator threads, publish
    telemetry or faults, execute actions, or modify the supplied plan.
    """

    invalid_rule_ids = frozenset(invalid_rule_ids)
    correlation = CorrelationEngine(bundle.signatures)
    work_items = {
        item.correlation_key: item
        for item in bundle.work_items.values()
        if item.rule_id not in invalid_rule_ids
    }
    event_results = []
    rule_errors = {}
    rule_info = {}
    decisions = {}
    failed = False

    for template in bundle.templates.values():
        base = template.item
        if base.rule_id in invalid_rule_ids:
            continue
        try:
            adapter = adapters.get("dse")
            if adapter is None:
                raise ValueError(
                    "no adapter is registered for source type 'dse'"
                )
            expansion = adapter.expand(template)
            expanded = work_items_for_dse_expansion(template, expansion)
        except Exception as error:
            failed = True
            reason = str(error)
            event_results.append(
                _event_result(
                    base,
                    "EXPANSION_ERROR",
                    stage="expansion",
                    component=None,
                    error=reason,
                )
            )
            rule_errors.setdefault((base.rule_id, None), reason)
            rule_info[(base.rule_id, None)] = base.rule_name
            continue

        if not expanded:
            failed = True
            reason = "DSE expansion discovered no instances"
            event_results.append(
                _event_result(
                    base,
                    "UNQUALIFIED",
                    stage="expansion",
                    component=None,
                    instance_count=0,
                    reason=reason,
                )
            )
            rule_errors.setdefault((base.rule_id, None), reason)
            rule_info[(base.rule_id, None)] = base.rule_name
            continue

        dse_instances = {
            item.component_name
            for item in expanded
            if item.dse_binding is not None
        }
        event_results.append(
            _event_result(
                base,
                "EXPANDED",
                stage="expansion",
                component=None,
                instance_count=len(dse_instances),
            )
        )
        for item in expanded:
            correlation.register_work_item(
                template.signature, item, "e2e-qualification"
            )
            # More than one DSE template may clone the same common predicate
            # into the same component scope.  Runtime identity is the
            # correlation key, so qualify it once as well.
            work_items.setdefault(item.correlation_key, item)

    sequence = 0
    for item in work_items.values():
        identity = (item.rule_id, item.component_name)
        rule_info[identity] = item.rule_name
        try:
            adapter = adapters.get(item.source_type)
            if adapter is None:
                raise ValueError(
                    "no adapter is registered for source type {!r}".format(
                        item.source_type
                    )
                )
            evaluated = adapter.collect(item)
            if not isinstance(evaluated, EvaluationResult):
                raise TypeError(
                    "adapter collect() must return EvaluationResult"
                )
            state = evaluated.result.value
        except Exception as error:
            failed = True
            reason = str(error)
            rule_errors.setdefault(identity, reason)
            event_results.append(
                _event_result(item, "EXECUTION_ERROR", error=reason)
            )
            continue

        details = {}
        if evaluated.error:
            details["error"] = evaluated.error
        event_results.append(_event_result(item, state, **details))
        if evaluated.result not in (
            EvaluationResultType.MATCH,
            EvaluationResultType.NO_MATCH,
        ):
            failed = True
            rule_errors.setdefault(
                identity,
                evaluated.error or state,
            )
            continue

        sequence += 1
        timestamp = time.time()
        evidence = FaultEvidenceEvent(
            signature_id=item.rule_id,
            event_id=item.event_id,
            component_name=item.component_name,
            source_id=item.source_id,
            correlation_key=item.correlation_key,
            monitor_id="e2e-qualification",
            plan_generation="e2e-qualification",
            work_state_generation=0,
            sequence=sequence,
            event_timestamp=timestamp,
            enqueue_timestamp=timestamp,
            result=evaluated,
        )
        try:
            decision = correlation.consume(evidence)
        except Exception as error:
            failed = True
            reason = str(error)
            rule_errors.setdefault(identity, reason)
            continue
        if decision is None:
            failed = True
            rule_errors.setdefault(
                identity,
                "rule correlation did not accept the event",
            )
            continue
        decisions[identity] = decision

    rule_results = []
    for identity in sorted(
        rule_info,
        key=lambda item: (item[0], "" if item[1] is None else item[1]),
    ):
        rule_id, component = identity
        result = {
            "rule": rule_info[identity],
            "rule_id": rule_id,
            "component": component,
            "stage": "rule_logic",
        }
        if identity in rule_errors:
            result.update(
                state="UNQUALIFIED",
                reason=rule_errors[identity],
            )
        else:
            decision = decisions[identity]
            result["state"] = "MATCH" if decision.active else "NO_MATCH"
        rule_results.append(result)

    return E2EQualificationResult(
        event_results=tuple(event_results),
        rule_results=tuple(rule_results),
        failed=failed,
    )
