"""Side-effect-free activation checks shared by the service and CLI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Optional, Tuple

from .adapters import DataSourceAdapter, VendorAdapter, adapter_map, require_adapter
from .hooks import VendorHookError, operation_hook_name
from .models import BrokenRule, ValidationIssue, ValidationResult
from .planner import PlanBundle, build_plans
from .rule_schema.errors import bound_diagnostic
from .validation import source_line_for_path


@dataclass(frozen=True)
class ActivationPreflightFailure:
    rule_id: int
    rule_name: str
    rule_version: str
    correlation_key: str
    code: str
    message: str

    @classmethod
    def from_metadata(
        cls, metadata, correlation_key, error, code="activation_preflight_failed",
    ):
        return cls(
            metadata.id, metadata.name, getattr(metadata, "version", ""), correlation_key,
            code, str(error),
        )


@dataclass(frozen=True)
class ActivationPreflightResult:
    validation: ValidationResult
    plan: PlanBundle
    adapters: Mapping[str, DataSourceAdapter]
    failures: Tuple[ActivationPreflightFailure, ...]

    @property
    def invalid_rule_ids(self):
        return frozenset(failure.rule_id for failure in self.failures)

    def __getattr__(self, name):
        return getattr(self.validation, name)

    def plan_for_generation(self, generation: str):
        """Bind the validated plan to the activated rules checksum."""

        return replace(
            self.plan,
            monitor_plans={key: replace(value, plan_generation=generation)
                           for key, value in self.plan.monitor_plans.items()},
            signatures={key: replace(value, plan_generation=generation)
                        for key, value in self.plan.signatures.items()},
        )


def validation_with_preflight_failures(
    validation: ValidationResult,
    failures,
    message_limit: Optional[int] = None,
):
    """Reject a candidate that references any unavailable installed hook."""

    failures_by_id: dict[int, ActivationPreflightFailure] = {}
    for failure in failures:
        failures_by_id.setdefault(failure.rule_id, failure)
    if not failures_by_id:
        return validation

    line = source_line_for_path(validation.source_lines, "$.signatures")
    issues = []
    broken = []
    for failure in failures_by_id.values():
        message = str(failure.message)
        if message_limit is not None:
            message = bound_diagnostic(message, message_limit)
        issue = ValidationIssue(
            "file", getattr(failure, "code", "activation_preflight_failed"),
            message, "$.signatures", failure.rule_name, failure.rule_id, line,
        )
        issues.append(issue)
        broken.append(
            BrokenRule(
                failure.rule_name,
                failure.rule_id,
                (issue,),
                getattr(failure, "rule_version", ""),
            )
        )
    return replace(
        validation,
        materialized_rules=(),
        file_errors=validation.file_errors + tuple(issues),
        broken_rules=validation.broken_rules + tuple(broken),
    )


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
            vendor_hooks.get(operation_hook_name(operation))
    if actions.log_collection is not None:
        for query in actions.log_collection.queries:
            if callable(query.executor) or query.type == "cli":
                continue
            vendor_hooks.get(operation_hook_name(query))


def build_adapter_registry(extensions) -> Mapping:
    """Build the canonical adapter set for one installed platform extension."""

    adapters = adapter_map(hooks=extensions.vendor_hooks)
    for source_type in extensions.dse_registry.source_types:
        adapters[source_type] = VendorAdapter(source_type, extensions.vendor_hooks)
    return adapters


def preflight_activation(
    validation: ValidationResult,
    extensions,
    polling_intervals: Mapping[str, float],
    failure_message_limit: Optional[int] = None,
) -> ActivationPreflightResult:
    """Build and validate runtime dispatch without reading a source."""

    materialized_rules = tuple(validation.materialized_rules)
    plan = build_plans(materialized_rules, "validation", polling_intervals)
    adapters = build_adapter_registry(extensions)
    vendor_hooks = extensions.vendor_hooks

    metadata_by_id = {
        rule.signature.metadata.id: rule.signature.metadata
        for rule in materialized_rules
    }
    failures_by_rule: dict[int, ActivationPreflightFailure] = {}
    for rule in materialized_rules:
        metadata = rule.signature.metadata
        try:
            validate_runtime_operation_hooks(rule, vendor_hooks)
        except (ValueError, VendorHookError) as error:
            failures_by_rule.setdefault(
                metadata.id,
                ActivationPreflightFailure.from_metadata(
                    metadata, "rule:{}".format(metadata.id), error
                ),
            )

    preflight_items = dict(plan.work_items)
    for template in plan.templates.values():
        for item in template.common_items:
            preflight_items.setdefault(item.correlation_key, item)
    for item in preflight_items.values():
        if item.rule_id in failures_by_rule:
            continue
        try:
            require_adapter(adapters, item.source_type).validate(item)
        except (ValueError, VendorHookError) as error:
            metadata = metadata_by_id[item.rule_id]
            failures_by_rule.setdefault(
                item.rule_id,
                ActivationPreflightFailure.from_metadata(
                    metadata, item.correlation_key, error
                ),
            )
    failures = tuple(failures_by_rule.values())
    if failures:
        validation = validation_with_preflight_failures(
            validation, failures, failure_message_limit
        )
        plan = PlanBundle({}, {}, {}, {})
    return ActivationPreflightResult(
        validation=validation,
        plan=plan,
        adapters=adapters,
        failures=failures,
    )
