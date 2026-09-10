"""Translate validated rules into immutable monitor execution plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from queue import Queue
from typing import Dict, Iterable, Mapping, Tuple

from .correlation import SignatureExecution
from .models import MaterializedRule
from .runtime import (
    MonitorExecutionPlan,
    MonitorWorkItem,
    MonitorWorkStateRecord,
    DSEWorkTemplate,
    make_correlation_key,
)


_MONITOR_BY_SOURCE = {
    "redis": "redis",
    "file": "file",
}


def monitor_type_for_source(source_type: str) -> str:
    """Return the single monitor routing decision for a source type."""

    return _MONITOR_BY_SOURCE.get(source_type, "common")


@dataclass(frozen=True)
class PlanBundle:
    monitor_plans: Mapping[str, MonitorExecutionPlan]
    signatures: Mapping[Tuple[int, str], SignatureExecution]
    work_items: Mapping[str, MonitorWorkItem]
    templates: Mapping[str, DSEWorkTemplate]


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
            for key in sorted(value, key=str)
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
    # Avoid repr() identities that can include memory addresses.
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
        "value_configs": configs.as_payload(),
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


def _build_work_item(
    signature,
    event,
    *,
    component_name,
    correlation_key,
    source_id,
    source_type,
    source,
    source_index,
    value_config,
    sampling_interval,
    common_predicate=False,
    dse_context=None,
    dse_binding=None,
    dse_source_handle=None,
    dse_evaluation_handle=None,
):
    """Build one work item from the shared signature/event fields."""

    metadata = signature.metadata
    return MonitorWorkItem(
        rule_id=metadata.id,
        rule_name=metadata.name,
        rule_version=metadata.version,
        schema_version=signature.schema_version,
        severity=metadata.severity,
        priority=metadata.priority,
        symptom=metadata.symptom,
        error_type=metadata.error_type,
        component_type=metadata.component,
        component_name=component_name,
        event_id=event.id,
        correlation_key=correlation_key,
        source_id=source_id,
        source_type=source_type,
        source=source,
        evaluation=_evaluation_mapping(event, source_index),
        match_count=event.match_count,
        match_period=event.match_period,
        value_config=value_config,
        common_predicate=common_predicate,
        sampling_interval=sampling_interval,
        sampling_interval_is_explicit=event.sampling_interval is not None,
        async_collection=event.async_collection,
        dse_context=dse_context,
        dse_binding=dse_binding,
        dse_source_handle=dse_source_handle,
        dse_evaluation_handle=dse_evaluation_handle,
    )


def work_items_for_dse_expansion(template, expansion_result):
    """Materialize one DSE expansion exactly as the runtime monitor does.

    The helper is shared by the monitor and the explicit end-to-end CLI mode
    so qualification cannot silently exercise a different binding shape from
    the live daemon.
    """

    items = []
    base = template.item
    static_work_keys = frozenset(template.static_work_keys)
    for binding in expansion_result.bindings:
        source_id = "dse:{}:{}".format(
            template.source_handle.reference.canonical,
            binding.source_id,
        )
        key = make_correlation_key(
            base.rule_id,
            base.event_id,
            binding.instance,
            base.symptom,
            source_id,
        )
        value_config = base.value_config.with_fallback(binding.value_configs)
        source = dict(binding.data)
        source["dse_reference"] = (
            template.source_handle.reference.canonical
        )
        items.append(
            replace(
                base,
                component_name=binding.instance,
                correlation_key=key,
                source_id=source_id,
                source=source,
                value_config=value_config,
                dse_binding=binding,
            )
        )

        # Clone common predicates into each expanded component scope.
        for common in template.common_items:
            common_key = make_correlation_key(
                common.rule_id,
                common.event_id,
                binding.instance,
                common.symptom,
                common.source_id,
            )
            if common_key in static_work_keys:
                continue
            items.append(
                replace(
                    common,
                    component_name=binding.instance,
                    correlation_key=common_key,
                )
            )
    return tuple(items)


def build_plans(
    materialized_rules: Iterable[MaterializedRule],
    plan_generation: str,
    polling_intervals: Mapping[str, float],
) -> PlanBundle:
    """Materialize immutable work and owning monitor plans for one generation."""

    materialized_rules = tuple(materialized_rules)
    if not materialized_rules:
        raise ValueError("materialized rules cannot be empty")
    schema_versions = {
        rule.signature.schema_version for rule in materialized_rules
    }
    if len(schema_versions) > 1:
        raise ValueError(
            "materialized rules from different schema versions cannot share a plan"
        )
    all_items: Dict[str, MonitorWorkItem] = {}
    grouped: Dict[str, Dict[str, MonitorWorkItem]] = {
        "redis": {},
        "file": {},
        "common": {},
    }
    signatures: Dict[Tuple[int, str], SignatureExecution] = {}
    templates: Dict[str, DSEWorkTemplate] = {}
    common_by_rule: Dict[int, Dict[Tuple[int, str], MonitorWorkItem]] = {}
    static_work_keys: Dict[int, list] = {}
    for materialized in materialized_rules:
        signature = materialized.signature
        metadata = signature.metadata
        has_runtime_dse = any(
            event.dse_source_handle is not None for event in materialized.events
        )
        resolved_instances = {
            _component_name(source.instance, metadata.component)
            for materialized_event in materialized.events
            for source in materialized_event.sources
            if source.instance
        }
        if not resolved_instances and not has_runtime_dse:
            resolved_instances = {metadata.component}

        items_by_instance: Dict[str, Dict[int, list[str]]] = {
            instance: {} for instance in resolved_instances
        }
        for materialized_event in materialized.events:
            event = materialized_event.event
            dse_context = materialized_event.dse_context
            if materialized_event.dse_source_handle is not None:
                handle = materialized_event.dse_source_handle
                template_id = "{}:{}:{}".format(
                    metadata.id, event.id, handle.reference.canonical
                )
                interval = float(
                    event.sampling_interval
                    if event.sampling_interval is not None
                    else polling_intervals["common"]
                )
                base_item = _build_work_item(
                    signature,
                    event,
                    component_name=metadata.component,
                    correlation_key="template:" + template_id,
                    source_id=handle.reference.canonical,
                    source_type="dse",
                    source={"reference": handle.reference.canonical},
                    source_index=0,
                    value_config=event.evaluation.value_configs,
                    sampling_interval=interval,
                    dse_context=dse_context,
                    dse_source_handle=handle,
                    dse_evaluation_handle=(
                        materialized_event.dse_evaluation_handle
                    ),
                )
                template = DSEWorkTemplate(
                    template_id=template_id,
                    item=base_item,
                    signature=signature,
                    source_handle=handle,
                    evaluation_handle=(
                        materialized_event.dse_evaluation_handle
                    ),
                )
                templates[template_id] = template
                continue
            for source_index, source in enumerate(materialized_event.sources):
                source_mapping = _source_mapping(source)
                source_id = _source_identity(source)
                monitor_type = monitor_type_for_source(source.type)
                config = event.evaluation.value_configs.with_fallback(
                    source.value_configs
                )
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
                targets = (
                    [_component_name(source.instance, metadata.component)]
                    if source.instance
                    else sorted(resolved_instances)
                )
                prototype_only = not source.instance and not targets
                if prototype_only:
                    targets = [metadata.component]
                for instance in targets:
                    key = make_correlation_key(
                        metadata.id, event.id, instance, metadata.symptom, source_id
                    )
                    item = _build_work_item(
                        signature,
                        event,
                        component_name=instance,
                        correlation_key=key,
                        source_id=source_id,
                        source_type=source.type,
                        source=source_mapping,
                        source_index=source_index,
                        value_config=config,
                        common_predicate=source.instance is None,
                        sampling_interval=float(
                            event.sampling_interval
                            if event.sampling_interval is not None
                            else polling_intervals[monitor_type]
                        ),
                        dse_context=dse_context,
                        dse_evaluation_handle=(
                            materialized_event.dse_evaluation_handle
                        ),
                    )
                    if prototype_only:
                        identity = (event.id, source_id)
                        common = common_by_rule.setdefault(metadata.id, {})
                        existing = common.get(identity)
                        if existing is None or existing.correlation_key.startswith("prototype:"):
                            common[identity] = replace(
                                item, correlation_key="prototype:" + key
                            )
                        continue
                    grouped[monitor_type][key] = item
                    all_items[key] = item
                    static_work_keys.setdefault(metadata.id, []).append(key)
                    if item.common_predicate:
                        identity = (item.event_id, item.source_id)
                        common = common_by_rule.setdefault(metadata.id, {})
                        existing = common.get(identity)
                        if existing is None or existing.correlation_key.startswith("prototype:"):
                            common[identity] = item
                    items_by_instance[instance].setdefault(event.id, []).append(key)

        for instance, event_keys in items_by_instance.items():
            signatures[(metadata.id, instance)] = SignatureExecution(
                signature=signature,
                component_name=instance,
                event_keys={key: tuple(value) for key, value in event_keys.items()},
                plan_generation=plan_generation,
            )

    for template_id, template in tuple(templates.items()):
        rule_id = template.item.rule_id
        enriched = replace(
            template,
            common_items=tuple(common_by_rule.get(rule_id, {}).values()),
            static_work_keys=tuple(static_work_keys.get(rule_id, ())),
        )
        templates[template_id] = enriched

    plans = {}
    for monitor_type, items in grouped.items():
        monitor_templates = templates if monitor_type == "common" else {}
        if not items and not monitor_templates:
            continue
        plans[monitor_type] = MonitorExecutionPlan(
            monitor_id=monitor_type,
            monitor_type=monitor_type,
            polling_interval=float(polling_intervals[monitor_type]),
            plan_generation=plan_generation,
            items_by_key=items,
            state_by_key={key: MonitorWorkStateRecord() for key in items},
            control_queue=Queue(),
            templates_by_key=monitor_templates,
            polling_intervals=polling_intervals,
        )
    return PlanBundle(plans, signatures, all_items, templates)
