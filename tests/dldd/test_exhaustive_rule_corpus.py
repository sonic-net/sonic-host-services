"""Primary contracts for the checked-in DLDD rule examples."""

from __future__ import absolute_import

from collections.abc import Mapping
from pathlib import Path
from typing import get_args

from dldd.dse import (
    DSEHook,
    DSERegistry,
    DSESourceHandle,
    ResolvedCommand,
    ResolvedEvaluation,
)
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import ResolvedSource, ValueConfig
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.preflight import preflight_activation
from dldd.rule_schema.v0_0_1 import (
    BUILTIN_OPERATION_TYPES,
    EvaluationV001,
    EventV001,
)
from dldd.validation import (
    ExactCompatibilityMatcher,
    ValidationContext,
    load_rules,
)


FIXTURES = Path(__file__).parent / "fixtures"
POSITIVE_CORPUS = FIXTURES / "all-supported-rule-types.yaml"
DUT_EXTENSION_CORPUS = FIXTURES / "dut-unsupported-extension-rules.yaml"


def _events(signatures):
    return tuple(
        event
        for signature in signatures
        for event in signature.conditions.events
    )


def _operations(signatures, *, queries=False):
    result = []
    for signature in signatures:
        if queries:
            collection = signature.actions.log_collection
            if collection is not None:
                result.extend(collection.queries)
        else:
            local = signature.actions.repair_actions.local_actions
            if local is not None:
                result.extend(local.action_list)
    return tuple(result)


def _wire_types(discriminated_union):
    union = get_args(discriminated_union)[0]
    return frozenset(
        value
        for model in get_args(union)
        for value in get_args(model.model_fields["type"].annotation)
    )


def _unexpected_runtime_call(*unused_args, **unused_kwargs):
    raise AssertionError("activation invoked a runtime DSE or vendor operation")


class _CorpusDSEHook(DSEHook):
    """Resolve corpus contracts without implementing vendor runtime behavior."""

    def resolve_source(self, reference, context):
        del context
        if reference.function == "get_direct_value":
            return (
                ResolvedSource(
                    type="redis",
                    path={
                        "database": "STATE_DB",
                        "table": "DLDD_CORPUS",
                        "key": "DLDD_CORPUS|DIRECT",
                        "path": "value",
                    },
                    instance="DLDD_CORPUS_DIRECT",
                    value_configs=ValueConfig(type="float", unit="units"),
                ),
            )
        return DSESourceHandle(
            reference=reference,
            expand=_unexpected_runtime_call,
            get_value=_unexpected_runtime_call,
        )

    def resolve_evaluation(self, reference, context):
        del reference, context
        return ResolvedEvaluation(expected_value=10, operator="==")

    def resolve_action(self, command, context):
        del command, context
        return ResolvedCommand(executor=_unexpected_runtime_call)

    def resolve_query(self, command, context):
        del command, context
        return ResolvedCommand(executor=_unexpected_runtime_call)


class _CorpusVendorHook(VendorHook):
    def collect(self, operation):
        _unexpected_runtime_call(operation)

    def execute_action(self, action):
        _unexpected_runtime_call(action)


def _wire_result(path=POSITIVE_CORPUS):
    result = load_rules(path, materialize=False)
    assert result.file_valid, result.file_errors
    assert not result.broken_rules, result.broken_rules
    assert result.schema_version == "0.0.1"
    return result


def _materialization_context(signatures):
    action_types = {
        item.type
        for item in _operations(signatures)
        if item.type not in BUILTIN_OPERATION_TYPES
    }
    query_types = {
        item.type
        for item in _operations(signatures, queries=True)
        if item.type not in ("cli", "dse")
    }
    return ValidationContext(
        product_id="TEST-PRODUCT",
        software_version="TEST-SOFTWARE",
        require_compatibility_identity=True,
        dse_registry=DSERegistry(
            hook=_CorpusDSEHook(),
            action_types=action_types,
            query_types=query_types,
        ),
    )


def _vendor_hooks_for_corpus(signatures):
    hook_names = {"i2c"}
    for event in _events(signatures):
        if event.type == "platform_api" and isinstance(event.path, Mapping):
            hook_names.add(str(event.path["hook"]))
    for operation in _operations(signatures) + _operations(
        signatures, queries=True
    ):
        if operation.type not in BUILTIN_OPERATION_TYPES:
            hook_names.add(str(operation.options.get("hook", operation.type)))

    registry = VendorHookRegistry()
    hook = _CorpusVendorHook()
    for name in sorted(hook_names):
        registry.register(name, hook)
    return registry


def test_positive_corpus_materializes_and_preflights_without_runtime_calls():
    wire = _wire_result()
    signatures = wire.ruleset.signatures
    context = _materialization_context(signatures)

    materialized = load_rules(POSITIVE_CORPUS, context=context)

    assert materialized.activation_valid, materialized.broken_rules
    assert {
        item.signature.metadata.name for item in materialized.materialized_rules
    } == {item.metadata.name for item in signatures}

    preflight = preflight_activation(
        materialized,
        PlatformExtensions(
            identity=PlatformIdentity(
                platform="corpus",
                product_id="TEST-PRODUCT",
                software_version="TEST-SOFTWARE",
            ),
            dse_registry=context.dse_registry,
            vendor_hooks=_vendor_hooks_for_corpus(signatures),
            compatibility_matcher=ExactCompatibilityMatcher(),
        ),
        {"redis": 1, "file": 1, "common": 1},
    )
    assert not preflight.failures, preflight.failures


def test_positive_corpus_covers_primary_version_owned_wire_types():
    signatures = _wire_result().ruleset.signatures
    events = _events(signatures)

    assert frozenset(item.type for item in events) == _wire_types(EventV001)
    assert frozenset(item.evaluation.type for item in events) == _wire_types(
        EvaluationV001
    )

    actions = {item.type for item in _operations(signatures)}
    queries = {
        item.type for item in _operations(signatures, queries=True)
    }
    assert BUILTIN_OPERATION_TYPES <= actions
    assert actions - BUILTIN_OPERATION_TYPES
    assert {"cli", "dse"} <= queries
    assert queries - {"cli", "dse"}


def test_dut_extension_corpus_remains_wire_valid_and_qualification_scoped():
    signatures = _wire_result(DUT_EXTENSION_CORPUS).ruleset.signatures
    assert all(
        "dldd-dut-qualification" in item.metadata.tags
        for item in signatures
    )

    categories = [
        tag
        for item in signatures
        for tag in item.metadata.tags
        if tag.startswith("broken-")
    ]
    assert categories
    assert len(categories) == len(set(categories))
    assert any("extension-control" in item.metadata.tags for item in signatures)
