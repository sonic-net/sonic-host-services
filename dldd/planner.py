"""Translate validated rules into immutable monitor execution plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from queue import Queue
from typing import Dict, Iterable, Mapping, Tuple

from .correlation import SignatureExecution
from .models import MaterializedRule
from .runtime import (
    MonitorExecutionPlan,
    MonitorWorkItem,
    MonitorWorkStateRecord,
    ValueConfig,
    make_correlation_key,
)


_MONITOR_BY_SOURCE = {
    "redis": "redis",
    "file": "file",
    "platform_api": "common",
    "i2c": "common",
    "cli": "common",
    "sysfs": "common",
}


@dataclass(frozen=True)
class PlanBundle:
    monitor_plans: Mapping[str, MonitorExecutionPlan]
    signatures: Mapping[Tuple[int, str], SignatureExecution]
    work_items: Mapping[str, MonitorWorkItem]


def _component_name(instance, fallback: str) -> str:
    if not instance:
        return fallback
    return str(instance).split(":", 1)[0]


def _source_mapping(source) -> Mapping:
    path = source.path
    if isinstance(path, Mapping):
        result = dict(path)
    elif source.type == "platform_api":
        result = {"hook": "platform", "operation": path}
    else:
        result = {"value": path}
    result.update(dict(source.vendor_data))
    return result


def _canonical_binding_value(value):
    """Return a deterministic JSON-safe identity for a source binding."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_binding_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_binding_value(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if callable(value):
        return {
            "callable": "{}.{}".format(
                getattr(value, "__module__", type(value).__module__),
                getattr(value, "__qualname__", type(value).__qualname__),
            )
        }
    # Source bindings should normally be declarative primitives.  Retaining a
    # stable type marker is safer than using repr(), which commonly embeds a
    # process-specific memory address.
    return {"object_type": "{}.{}".format(type(value).__module__, type(value).__qualname__)}


def _source_identity(source) -> str:
    binding = {
        "instance": source.instance or "",
        "path": _canonical_binding_value(source.path),
        "vendor_data": _canonical_binding_value(source.vendor_data),
    }
    canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    return "{}:{}".format(source.type, canonical)


def _evaluation_mapping(event, source_index: int) -> Mapping:
    evaluation = event.evaluation
    configs = evaluation.value_configs
    expected_value = evaluation.value
    if evaluation.type == "comparison" and isinstance(expected_value, tuple):
        expected_value = expected_value[source_index]
    result = {
        "type": evaluation.type,
        "value": expected_value,
        "case_sensitive": evaluation.case_sensitive,
        "value_configs": {
            "type": configs.type,
            "unit": configs.unit,
            "scaling": configs.scaling,
            "encoding": configs.encoding,
        },
    }
    if evaluation.operator is not None:
        result["operator"] = evaluation.operator
    if evaluation.logic is not None:
        result["logic"] = evaluation.logic
    if evaluation.unit is not None:
        result["unit"] = evaluation.unit
    if evaluation.comparator is not None:
        result["comparator"] = evaluation.comparator
    return result


def build_plans(
    materialized_rules: Iterable[MaterializedRule],
    plan_generation: str,
    polling_intervals: Mapping[str, float],
) -> PlanBundle:
    all_items: Dict[str, MonitorWorkItem] = {}
    grouped: Dict[str, Dict[str, MonitorWorkItem]] = {
        "redis": {},
        "file": {},
        "common": {},
    }
    signatures: Dict[Tuple[int, str], SignatureExecution] = {}

    for materialized in materialized_rules:
        signature = materialized.signature
        metadata = signature.metadata
        resolved_instances = {
            _component_name(source.instance, metadata.component)
            for materialized_event in materialized.events
            for source in materialized_event.sources
            if source.instance
        }
        if not resolved_instances:
            resolved_instances = {metadata.component}

        items_by_instance = {instance: {} for instance in resolved_instances}
        for materialized_event in materialized.events:
            event = materialized_event.event
            for source_index, source in enumerate(materialized_event.sources):
                targets = (
                    [_component_name(source.instance, metadata.component)]
                    if source.instance
                    else sorted(resolved_instances)
                )
                for instance in targets:
                    source_mapping = _source_mapping(source)
                    source_id = _source_identity(source)
                    monitor_type = _MONITOR_BY_SOURCE.get(source.type)
                    if monitor_type is None:
                        # Vendor sources must materialize to an advertised adapter
                        # type before planning; they run in the common monitor.
                        monitor_type = "common"
                    key = make_correlation_key(
                        metadata.id, event.id, instance, metadata.symptom, source_id
                    )
                    config = source.value_configs or event.evaluation.value_configs
                    if config.type == "N/A" and event.evaluation.value_configs.type != "N/A":
                        config = event.evaluation.value_configs
                    if config.type == "N/A" and source_mapping.get("scaling") not in (
                        None,
                        "",
                        "N/A",
                    ):
                        config = type(config)(
                            type="float",
                            unit=str(source_mapping.get("unit", "N/A")),
                            scaling=source_mapping["scaling"],
                            encoding="N/A",
                        )
                    value_config = ValueConfig(
                        type=config.type,
                        unit=config.unit,
                        scaling=config.scaling,
                        encoding=config.encoding,
                    )
                    item = MonitorWorkItem(
                        rule_id=metadata.id,
                        rule_name=metadata.name,
                        rule_version=metadata.version,
                        schema_version="0.0.1",
                        severity=metadata.severity,
                        priority=metadata.priority,
                        symptom=metadata.symptom,
                        error_type=metadata.error_type,
                        component_type=metadata.component,
                        component_name=instance,
                        event_id=event.id,
                        correlation_key=key,
                        source_id=source_id,
                        source_type=source.type,
                        source=source_mapping,
                        evaluation=_evaluation_mapping(event, source_index),
                        match_count=event.match_count,
                        match_period=event.match_period,
                        value_config=value_config,
                        common_predicate=source.instance is None,
                        sampling_interval=float(
                            event.sampling_interval
                            if event.sampling_interval is not None
                            else polling_intervals[monitor_type]
                        ),
                        sampling_interval_is_explicit=(
                            event.sampling_interval is not None
                        ),
                        async_collection=event.async_collection,
                    )
                    grouped[monitor_type][key] = item
                    all_items[key] = item
                    items_by_instance[instance].setdefault(event.id, []).append(key)

        for instance, event_keys in items_by_instance.items():
            signatures[(metadata.id, instance)] = SignatureExecution(
                signature=signature,
                component_name=instance,
                event_keys={key: tuple(value) for key, value in event_keys.items()},
                plan_generation=plan_generation,
            )

    plans = {}
    for monitor_type, items in grouped.items():
        if not items:
            continue
        monitor_id = monitor_type
        plans[monitor_type] = MonitorExecutionPlan(
            monitor_id=monitor_id,
            monitor_type=monitor_type,
            polling_interval=float(polling_intervals[monitor_type]),
            plan_generation=plan_generation,
            items_by_key=items,
            state_by_key={key: MonitorWorkStateRecord() for key in items},
            control_queue=Queue(),
        )
    return PlanBundle(plans, signatures, all_items)
