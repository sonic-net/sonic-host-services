"""Build bounded operator-facing status for the active DLDD rule generation."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .rule_schema.errors import bound_diagnostic, bound_identity, bound_path
from .timestamps import floor_timestamp_fields


MAX_DETAILS = 4096
MAX_DETAILS_PER_RULE = 256
HEALTHY_WORK_STATES = frozenset(
    (
        "READY",
        "COLLECTING",
        "IN_FLIGHT",
        "HELD_BY_PRIMARY",
        "RECHECK_REQUESTED",
    )
)


def _reason(records: Iterable[Mapping[str, Any]]) -> str:
    reasons = []
    for record in records:
        reason = str(record.get("reason", ""))
        if reason and reason not in reasons:
            reasons.append(reason)
    if not reasons:
        return ""
    suffix = " (+{} more)".format(len(reasons) - 1) if len(reasons) > 1 else ""
    return bound_diagnostic(reasons[0] + suffix, 512)


def _monitor_indexes(monitors):
    state_by_key = {}
    plan_by_key = {}
    for monitor in tuple(monitors):
        plan = monitor.plan
        for key, state in tuple(plan.state_by_key.items()):
            state_by_key[key] = state
            plan_by_key[key] = plan
    return state_by_key, plan_by_key


def _templates_by_rule(monitors):
    result = defaultdict(list)
    for monitor in tuple(monitors):
        plan = monitor.plan
        for template_id, template in tuple(plan.templates_by_key.items()):
            result[template.item.rule_id].append(
                (
                    template,
                    plan.expansion_state_by_key.get(template_id),
                )
            )
    return result


def _active_components_by_rule(orchestrator):
    result = defaultdict(set)
    if orchestrator is None:
        return result
    for fault in getattr(orchestrator, "faults", {}).values():
        if getattr(fault, "status", "") == "ACTIVE":
            result[fault.rule_id].add(fault.component_name)
    return result


def _group_by_rule(values):
    result = defaultdict(list)
    for value in values:
        result[value.rule_id].append(value)
    return result


def _group_broken_by_rule(records):
    result = defaultdict(list)
    for record in records:
        rule_id = record.get("rule_id")
        if rule_id is not None:
            result[rule_id].append(record)
    return result


def _matching_broken(records):
    return {
        record.get("correlation_key"): record
        for record in records
        if record.get("correlation_key")
    }


def _health(state_names, has_runtime_failure):
    if not state_names:
        return "BROKEN"
    if all(state == "SUSPENDED" for state in state_names):
        return "SUSPENDED"
    if all(state == "BROKEN" for state in state_names):
        return "BROKEN"
    if has_runtime_failure or any(
        state in ("BROKEN", "DEGRADED", "SUSPENDED", "UNKNOWN")
        for state in state_names
    ):
        return "DEGRADED"
    return "OK"


def _work_item_detail(
    item,
    state,
    plan,
    broken_record,
    active_components,
    monotonic_now,
    wall_now,
):
    state_name = state.state.value if state is not None else "UNKNOWN"
    interval = (
        item.sampling_interval
        if item.sampling_interval_is_explicit or plan is None
        else plan.polling_interval
    )
    next_due = None
    if state is not None and state.next_sample_due is not None:
        next_due = wall_now + (state.next_sample_due - monotonic_now)
    return {
        "correlation_key": bound_path(item.correlation_key, 512),
        "event_id": item.event_id,
        "component": bound_identity(item.component_name, 128),
        "source_type": bound_identity(item.source_type, 64),
        "source_id": bound_identity(item.source_id, 256),
        "monitor": bound_identity(
            plan.monitor_type if plan is not None else "", 64
        ),
        "state": state_name,
        "sampling_interval": interval,
        "interval_source": (
            "event" if item.sampling_interval_is_explicit else "monitor_default"
        ),
        "async": item.async_collection,
        "active_fault": item.component_name in active_components,
        "last_attempt": (
            state.last_attempt_timestamp if state is not None else None
        ),
        "last_success": (
            state.last_success_timestamp if state is not None else None
        ),
        "next_due": next_due,
        "failure_count": (
            state.consecutive_failure_count
            if state is not None
            else int(broken_record.get("failure_count", 0) or 0)
        ),
        "reason": bound_diagnostic(
            str(broken_record.get("reason", "")), 512
        ),
    }


def _materialized_row(
    rule,
    items,
    records,
    state_by_key,
    plan_by_key,
    active_components,
    detail_budget,
    monotonic_now,
    wall_now,
    templates=(),
):
    state_names = []
    last_attempts = []
    last_successes = []
    failure_counts = [
        int(record.get("failure_count", 0) or 0) for record in records
    ]
    healthy_count = 0
    details = []
    broken_by_key = _matching_broken(records)

    for item in items:
        state = state_by_key.get(item.correlation_key)
        state_name = state.state.value if state is not None else "UNKNOWN"
        state_names.append(state_name)
        healthy_count += int(state_name in HEALTHY_WORK_STATES)
        if state is not None:
            if state.last_attempt_timestamp is not None:
                last_attempts.append(state.last_attempt_timestamp)
            if state.last_success_timestamp is not None:
                last_successes.append(state.last_success_timestamp)
            failure_counts.append(state.consecutive_failure_count)
        if len(details) < min(MAX_DETAILS_PER_RULE, detail_budget):
            details.append(
                _work_item_detail(
                    item,
                    state,
                    plan_by_key.get(item.correlation_key),
                    broken_by_key.get(item.correlation_key, {}),
                    active_components,
                    monotonic_now,
                    wall_now,
                )
            )

    record_attempts = [
        record.get("last_attempt")
        for record in records
        if record.get("last_attempt") is not None
    ]
    metadata = rule.signature.metadata
    expansion_errors = [
        state.last_error
        for unused_template, state in templates
        if state is not None and state.last_error
    ]
    expansion_phases = [
        state.phase
        for unused_template, state in templates
        if state is not None
    ]
    health = _health(state_names, bool(records) or bool(expansion_errors))
    reason = _reason(records)
    if not state_names and templates:
        health = "DEGRADED"
        reason = "dse_discovery_{}".format(
            expansion_phases[0].lower() if expansion_phases else "pending"
        )
    elif expansion_errors:
        health = "DEGRADED"
        reason = bound_diagnostic(expansion_errors[0], 512)
    return {
        "rule_id": metadata.id,
        "rule": bound_identity(metadata.name, 128),
        "version": bound_identity(metadata.version, 64),
        "component": bound_identity(metadata.component, 128),
        "health": health,
        "work_items_healthy": healthy_count,
        "work_items_total": len(items),
        "work_items_omitted": len(items) - len(details),
        "active_faults": len(active_components),
        "last_attempt": max(last_attempts + record_attempts, default=None),
        "last_success": max(last_successes, default=None),
        "failure_count": max(failure_counts, default=0),
        "reason": reason,
        "work_items": details,
    }


def _ingestion_rows(records, materialized_ids):
    grouped = defaultdict(list)
    for record in records:
        if record.get("rule_id") in materialized_ids:
            continue
        identity = (
            record.get("rule_id"),
            str(record.get("rule", "")),
            str(record.get("version", "")),
        )
        grouped[identity].append(record)

    rows = []
    for (rule_id, rule, version), failures in grouped.items():
        attempts = [
            failure.get("last_attempt")
            for failure in failures
            if failure.get("last_attempt") is not None
        ]
        rows.append(
            {
                "rule_id": rule_id,
                "rule": bound_identity(rule, 128),
                "version": bound_identity(version, 64),
                "component": "",
                "health": "BROKEN",
                "work_items_healthy": 0,
                "work_items_total": 0,
                "work_items_omitted": 0,
                "active_faults": 0,
                "last_attempt": max(attempts, default=None),
                "last_success": None,
                "failure_count": max(
                    (
                        int(failure.get("failure_count", 0) or 0)
                        for failure in failures
                    ),
                    default=0,
                ),
                "reason": _reason(failures),
                "work_items": [],
            }
        )
    return rows


def build_rule_status_snapshot(
    activation,
    orchestrator,
    monitors,
    monotonic_now: Optional[float] = None,
    wall_now: Optional[float] = None,
) -> Tuple[Tuple[Dict[str, Any], ...], bool]:
    """Return aggregate rule rows and whether work-item detail was omitted."""

    if activation is None:
        return (), False
    payload = getattr(activation, "payload", None)
    materialized = tuple(getattr(payload, "materialized_rules", ()) or ())
    work_items = (
        tuple(getattr(orchestrator, "work_items", {}).values())
        if orchestrator is not None
        else ()
    )
    runtime_broken = (
        tuple(getattr(orchestrator, "broken_rules", {}).values())
        if orchestrator is not None
        else ()
    )
    state_by_key, plan_by_key = _monitor_indexes(monitors)
    templates_by_rule = _templates_by_rule(monitors)
    items_by_rule = _group_by_rule(work_items)
    broken_by_rule = _group_broken_by_rule(runtime_broken)
    active_by_rule = _active_components_by_rule(orchestrator)
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    wall_now = time.time() if wall_now is None else wall_now

    rows = []
    detail_count = 0
    materialized_ids = set()
    for rule in sorted(materialized, key=lambda item: item.signature.metadata.id):
        rule_id = rule.signature.metadata.id
        materialized_ids.add(rule_id)
        items = sorted(
            items_by_rule.get(rule_id, ()),
            key=lambda item: item.correlation_key,
        )
        row = _materialized_row(
            rule,
            items,
            broken_by_rule.get(rule_id, ()),
            state_by_key,
            plan_by_key,
            active_by_rule.get(rule_id, set()),
            MAX_DETAILS - detail_count,
            monotonic_now,
            wall_now,
            templates_by_rule.get(rule_id, ()),
        )
        rows.append(row)
        detail_count += len(row["work_items"])

    rows.extend(
        _ingestion_rows(
            tuple(getattr(activation, "broken_rules", ()) or ()),
            materialized_ids,
        )
    )
    rows.sort(
        key=lambda item: (
            item["rule_id"] is None,
            item["rule_id"] or 0,
            item["rule"],
        )
    )
    detail_truncated = any(row["work_items_omitted"] for row in rows)
    return (
        tuple(floor_timestamp_fields(row) for row in rows),
        detail_truncated,
    )
