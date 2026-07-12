"""Side-effect-free activation checks shared by the service and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from .adapters import VendorAdapter, adapter_map
from .hooks import VendorHookError
from .planner import build_plans


@dataclass(frozen=True)
class ActivationPreflightFailure:
    rule_id: int
    rule_name: str
    rule_version: str
    correlation_key: str
    code: str
    message: str


@dataclass(frozen=True)
class ActivationPreflightResult:
    validation: object
    plan: object
    adapters: Mapping
    failures: Tuple[ActivationPreflightFailure, ...]

    @property
    def invalid_rule_ids(self):
        return frozenset(failure.rule_id for failure in self.failures)


def validate_runtime_operation_hooks(materialized_rule, vendor_hooks) -> None:
    """Ensure every materialized non-built-in operation has a runtime target."""

    actions = materialized_rule.signature.actions
    local = actions.repair_actions.local_actions
    if local is not None:
        for operation in local.action_list:
            if operation.type == "i2c":
                vendor_hooks.validate_i2c_source(operation.path)
                continue
            if callable(operation.executor) or operation.type == "cli":
                continue
            hook_name = str(operation.options.get("hook", operation.type))
            vendor_hooks.get(hook_name)
    if actions.log_collection is not None:
        for query in actions.log_collection.queries:
            if callable(query.executor) or query.type == "cli":
                continue
            hook_name = str(query.options.get("hook", query.type))
            vendor_hooks.get(hook_name)


def build_adapter_registry(extensions) -> Mapping:
    """Build the canonical adapter set for one installed platform extension."""

    adapters = adapter_map(hooks=extensions.vendor_hooks)
    for source_type in extensions.dse_registry.source_types:
        adapters[source_type] = VendorAdapter(
            source_type, extensions.vendor_hooks
        )
    return adapters


def preflight_activation(
    validation,
    extensions,
    polling_intervals: Mapping[str, float],
) -> ActivationPreflightResult:
    """Build and validate runtime dispatch without reading a source."""

    materialized_rules = tuple(validation.materialized_rules)
    plan = build_plans(
        materialized_rules,
        "validation",
        polling_intervals,
    )
    adapters = build_adapter_registry(extensions)
    vendor_hooks = extensions.vendor_hooks

    rules = materialized_rules
    metadata_by_id = {
        rule.signature.metadata.id: rule.signature.metadata for rule in rules
    }
    failures = {}
    for rule in rules:
        metadata = rule.signature.metadata
        try:
            validate_runtime_operation_hooks(rule, vendor_hooks)
        except (ValueError, VendorHookError) as error:
            failures.setdefault(
                metadata.id,
                ActivationPreflightFailure(
                    rule_id=metadata.id,
                    rule_name=metadata.name,
                    rule_version=metadata.version,
                    correlation_key="rule:{}".format(metadata.id),
                    code="activation_preflight_failed",
                    message=str(error),
                ),
            )

    preflight_items = dict(plan.work_items)
    for template in plan.templates.values():
        for item in template.common_items:
            preflight_items.setdefault(item.correlation_key, item)
    for item in preflight_items.values():
        if item.rule_id in failures:
            continue
        try:
            adapter = adapters.get(item.source_type)
            if adapter is None:
                raise ValueError(
                    "no adapter is registered for source type {!r}".format(
                        item.source_type
                    )
                )
            adapter.validate(item)
        except (ValueError, VendorHookError) as error:
            metadata = metadata_by_id[item.rule_id]
            failures.setdefault(
                item.rule_id,
                ActivationPreflightFailure(
                    rule_id=item.rule_id,
                    rule_name=metadata.name,
                    rule_version=metadata.version,
                    correlation_key=item.correlation_key,
                    code="activation_preflight_failed",
                    message=str(error),
                ),
            )
            continue

    return ActivationPreflightResult(
        validation=validation,
        plan=plan,
        adapters=adapters,
        failures=tuple(failures.values()),
    )
