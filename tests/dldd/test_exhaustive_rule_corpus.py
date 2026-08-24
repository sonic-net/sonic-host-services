"""Contract checks for the checked-in portable DLDD rule corpus.

The corpus is operator-facing example data, not another schema authority.  The
tests therefore derive finite wire values from the installed version-owned
Pydantic models and exercise its materialization and preflight paths.  Adding
a new example must not require updating a signature count or a tag registry.
"""

from __future__ import absolute_import

from collections.abc import Mapping
from pathlib import Path
from typing import get_args

from dldd.adapters import FileAdapter
from dldd.dse import (
    DSEBinding,
    DSEContext,
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSEHook,
    DSEInvocationContext,
    DSEReferenceError,
    DSERegistry,
    DSESourceHandle,
    ResolvedCommand,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import ResolvedSource, ValueConfig
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.planner import build_plans
from dldd.preflight import preflight_activation
from dldd.rule_schema.v0_0_1 import (
    BUILTIN_OPERATION_TYPES,
    ComparisonEvaluationV001,
    DSEEvaluationV001,
    EvaluationV001,
    EventV001,
    MaskEvaluationV001,
    SeverityType,
    StringEvaluationV001,
    SymptomType,
    ValueConfigV001,
)
from dldd.validation import (
    ExactCompatibilityMatcher,
    ValidationContext,
    load_rules,
)


FIXTURES = Path(__file__).parent / "fixtures"
POSITIVE_CORPUS = FIXTURES / "all-supported-rule-types.yaml"
DUT_EXTENSION_CORPUS = FIXTURES / "dut-unsupported-extension-rules.yaml"


def _union_models(discriminated_union):
    """Return Pydantic models from an ``Annotated[Union[...], ...]``."""

    union = get_args(discriminated_union)[0]
    return get_args(union)


def _literal_values(model, field):
    return frozenset(get_args(model.model_fields[field].annotation))


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
            continue
        local = signature.actions.repair_actions.local_actions
        if local is not None:
            result.extend(local.action_list)
    return tuple(result)


class _CorpusDSEHook(DSEHook):
    """Side-effect-free resolver for materializing positive corpus examples."""

    def __init__(self):
        self.source_modes = {}
        self.source_handles = {}
        self.evaluation_modes = {}
        self.action_commands = []
        self.query_commands = []
        self.expand_calls = []
        self.get_value_calls = []

    def resolve_source(self, reference, context):
        del context
        if reference.function == "get_direct_value":
            self.source_modes[reference.canonical] = "direct"
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
        self.source_modes[reference.canonical] = "runtime"
        handle = DSESourceHandle(
            reference=reference,
            expand=self._expander(reference),
            get_value=self._value_reader(reference),
        )
        self.source_handles[reference.canonical] = handle
        return handle

    def _expander(self, reference):
        def expand(unused_context):
            self.expand_calls.append(reference.canonical)
            selector = reference.selector
            if selector == "empty_sensor*":
                return DSEExpansionResult(())
            if selector == "multi_source_sensor*":
                return DSEExpansionResult(
                    (
                        DSEBinding("DLDD_CORPUS_MULTI", "source-a"),
                        DSEBinding("DLDD_CORPUS_MULTI", "source-b"),
                    )
                )
            binding = DSEBinding(
                "DLDD_CORPUS_{}".format(selector.rstrip("*").upper()),
                reference.canonical,
            )
            return DSEExpansionResult(
                (binding,),
                authoritative=selector == "authoritative_sensor*",
            )

        return expand

    def _value_reader(self, reference):
        def get_value(unused_invocation):
            self.get_value_calls.append(reference.canonical)
            return 0

        return get_value

    def resolve_evaluation(self, reference, context):
        del context
        if reference.function == "get_fixed_threshold":
            self.evaluation_modes[reference.canonical] = "fixed"
            return ResolvedEvaluation(
                expected_value=10,
                operator=">=",
                value_configs=ValueConfig(type="float", unit="units"),
            )
        if reference.function == "get_trusted_comparator":
            self.evaluation_modes[reference.canonical] = "trusted-comparator"
            return DSEEvaluationHandle(
                reference=reference,
                get_comparator=lambda unused_invocation: ResolvedEvaluation(
                    comparator=lambda actual: actual == "trusted-match"
                ),
            )
        self.evaluation_modes[reference.canonical] = "runtime"
        return DSEEvaluationHandle(
            reference=reference,
            get_comparator=lambda unused_invocation: ResolvedEvaluation(
                expected_value=0,
                operator="==",
            ),
        )

    def resolve_action(self, command, context):
        del context
        self.action_commands.append(command)
        return ResolvedCommand(executor=lambda unused_operation: {})

    def resolve_query(self, command, context):
        del context
        self.query_commands.append(command)
        return ResolvedCommand(executor=lambda unused_operation: {})


class _CorpusVendorHook(VendorHook):
    """Preflight-only hook proving activation performs no runtime calls."""

    def __init__(self):
        self.collect_calls = []
        self.action_calls = []
        self.validated_sources = []

    def collect(self, operation):
        self.collect_calls.append(operation)
        return None

    def execute_action(self, action):
        self.action_calls.append(action)
        return {}

    def validate_source(self, operation):
        self.validated_sources.append(operation)


def _wire_result(path=POSITIVE_CORPUS):
    result = load_rules(path, materialize=False)
    assert result.file_valid, result.file_errors
    assert not result.broken_rules, result.broken_rules
    assert result.ruleset is not None
    return result


def _materialization_context(signatures):
    vendor_actions = {
        operation.type
        for operation in _operations(signatures)
        if operation.type not in BUILTIN_OPERATION_TYPES
    }
    vendor_queries = {
        operation.type
        for operation in _operations(signatures, queries=True)
        if operation.type not in ("cli", "dse")
    }
    hook = _CorpusDSEHook()
    context = ValidationContext(
        product_id="TEST-PRODUCT",
        software_version="TEST-SOFTWARE",
        require_compatibility_identity=True,
        dse_registry=DSERegistry(
            hook=hook,
            action_types=vendor_actions,
            query_types=vendor_queries,
        ),
    )
    return context, hook


def _vendor_hooks_for_corpus(signatures):
    hook_names = {"i2c"}
    for event in _events(signatures):
        if event.type == "platform_api" and isinstance(event.path, Mapping):
            hook_names.add(str(event.path["hook"]))
    for operation in _operations(signatures) + _operations(
        signatures, queries=True
    ):
        if operation.type in BUILTIN_OPERATION_TYPES:
            continue
        hook_names.add(str(operation.options.get("hook", operation.type)))

    hook = _CorpusVendorHook()
    registry = VendorHookRegistry()
    for name in sorted(hook_names):
        registry.register(name, hook)
    return registry, hook


def _assert_dse_resolution_modes(validation, plans, hook):
    """Prove direct, runtime, fixed, and trusted DSE modes execute."""

    assert {"direct", "runtime"} <= set(hook.source_modes.values())
    assert {"fixed", "trusted-comparator", "runtime"} <= set(
        hook.evaluation_modes.values()
    )
    assert plans.work_items
    assert plans.templates
    assert any(
        item.dse_evaluation_handle is None
        and item.evaluation.get("type") == "dse"
        and item.evaluation.get("value") == 10
        for item in plans.work_items.values()
    )

    materialized_events = [
        event
        for rule in validation.materialized_rules
        for event in rule.events
    ]
    trusted_handles = [
        event.dse_evaluation_handle
        for event in materialized_events
        if event.dse_evaluation_handle is not None
        and event.dse_evaluation_handle.reference.function
        == "get_trusted_comparator"
    ]
    assert trusted_handles
    resolved = trusted_handles[0].get_comparator(
        DSEInvocationContext(
            rule=next(
                event.dse_context
                for event in materialized_events
                if event.dse_evaluation_handle is trusted_handles[0]
            ),
            binding=DSEBinding(
                instance="DLDD_CORPUS_TRUSTED",
                source_id="trusted",
            ),
        )
    )
    assert resolved.operator is None
    assert callable(resolved.comparator)
    assert resolved.comparator("trusted-match")
    assert not resolved.comparator("other")
    assert hook.action_commands
    assert hook.query_commands


def _is_canonical_dse_command(command):
    try:
        parse_reference(command)
    except DSEReferenceError:
        return False
    return True


def _assert_operation_and_artifact_shapes(signatures, actions, queries):
    cli_actions = [item for item in actions if item.type == "cli"]
    assert any(
        item.argv and item.timeout is not None and item.max_output_bytes is not None
        for item in cli_actions
    )

    i2c_actions = [item for item in actions if item.type == "i2c"]
    assert {item.path["i2c_type"] for item in i2c_actions} == {"get", "set"}
    assert all(item.path["size"] in {"b", "w", "l"} for item in i2c_actions)
    assert all(
        "value" in item.path
        for item in i2c_actions
        if item.path["i2c_type"] == "set"
    )

    for operations in (actions, queries):
        dse_commands = [
            item.command for item in operations if item.type == "dse"
        ]
        assert any(_is_canonical_dse_command(item) for item in dse_commands)
        assert any(not _is_canonical_dse_command(item) for item in dse_commands)

    assert any(item.type not in BUILTIN_OPERATION_TYPES for item in actions)
    assert any(item.type not in ("cli", "dse") for item in queries)
    assert any(
        item.argv and item.timeout is not None
        for item in queries
        if item.type == "cli"
    )
    assert any(
        signature.actions.log_collection is not None
        and signature.actions.log_collection.logs
        for signature in signatures
    )
    assert any(
        ":" in action
        for signature in signatures
        for action in (
            signature.actions.repair_actions.remote_actions.action_list
        )
    )


def test_positive_corpus_is_valid_materializable_and_safe_to_preflight():
    wire = _wire_result()
    signatures = wire.ruleset.signatures
    names = {signature.metadata.name for signature in signatures}

    assert all("dldd-corpus" in item.metadata.tags for item in signatures)

    context, hook = _materialization_context(signatures)
    materialized = load_rules(
        POSITIVE_CORPUS,
        context=context,
    )
    assert materialized.file_valid, materialized.file_errors
    assert not materialized.broken_rules, materialized.broken_rules
    assert {
        item.signature.metadata.name for item in materialized.materialized_rules
    } == names

    plans = build_plans(
        materialized.materialized_rules,
        "corpus",
        {"redis": 1, "file": 1, "common": 1},
    )
    _assert_dse_resolution_modes(materialized, plans, hook)

    vendor_hooks, vendor_hook = _vendor_hooks_for_corpus(
        materialized.ruleset.signatures
    )
    preflight = preflight_activation(
        materialized,
        PlatformExtensions(
            identity=PlatformIdentity(
                platform="corpus",
                product_id="TEST-PRODUCT",
                software_version="TEST-SOFTWARE",
            ),
            dse_registry=context.dse_registry,
            vendor_hooks=vendor_hooks,
            compatibility_matcher=ExactCompatibilityMatcher(),
        ),
        {"redis": 1, "file": 1, "common": 1},
    )
    assert not preflight.failures, preflight.failures
    assert vendor_hook.validated_sources
    assert not vendor_hook.collect_calls
    assert not vendor_hook.action_calls
    assert not hook.expand_calls
    assert not hook.get_value_calls
    _assert_expansion_variants(hook)


def _assert_expansion_variants(hook):
    handles = {
        handle.reference.selector: handle
        for handle in hook.source_handles.values()
    }
    expected_selectors = {
        "empty_sensor*",
        "authoritative_sensor*",
        "nonauthoritative_sensor*",
        "multi_source_sensor*",
    }
    assert expected_selectors <= set(handles)

    empty = handles["empty_sensor*"].expand(DSEContext())
    authoritative = handles["authoritative_sensor*"].expand(DSEContext())
    nonauthoritative = handles["nonauthoritative_sensor*"].expand(DSEContext())
    multiple = handles["multi_source_sensor*"].expand(DSEContext())

    assert not empty.bindings and not empty.authoritative
    assert authoritative.bindings and authoritative.authoritative
    assert nonauthoritative.bindings and not nonauthoritative.authoritative
    assert not multiple.authoritative
    assert len({item.instance for item in multiple.bindings}) == 1
    assert len({item.source_id for item in multiple.bindings}) > 1

    binding = authoritative.bindings[0]
    assert handles["authoritative_sensor*"].get_value(
        DSEInvocationContext(rule=DSEContext(), binding=binding)
    ) == 0
    assert set(hook.expand_calls) == {
        handles[selector].reference.canonical
        for selector in expected_selectors
    }
    assert hook.get_value_calls == [
        handles["authoritative_sensor*"].reference.canonical
    ]


def test_positive_corpus_covers_version_owned_finite_wire_values():
    signatures = _wire_result().ruleset.signatures
    events = _events(signatures)
    event_models = _union_models(EventV001)
    evaluation_models = _union_models(EvaluationV001)

    expected_sources = frozenset(
        value
        for model in event_models
        for value in _literal_values(model, "type")
    )
    expected_evaluations = frozenset(
        value
        for model in evaluation_models
        for value in _literal_values(model, "type")
    )
    assert frozenset(event.type for event in events) == expected_sources
    assert frozenset(event.evaluation.type for event in events) == (
        expected_evaluations
    )

    for model in (
        ComparisonEvaluationV001,
        StringEvaluationV001,
        DSEEvaluationV001,
    ):
        evaluation_type = next(iter(_literal_values(model, "type")))
        observed = {
            event.evaluation.operator
            for event in events
            if event.evaluation.type == evaluation_type
        }
        expected = set(_literal_values(model, "operator"))
        if model is DSEEvaluationV001:
            expected.add(None)
        assert observed == expected, evaluation_type

    mask_logic = _literal_values(MaskEvaluationV001, "logic")
    assert {
        event.evaluation.logic
        for event in events
        if event.evaluation.type == "mask"
    } == mask_logic
    assert {
        event.evaluation.value_configs.type for event in events
    } == _literal_values(ValueConfigV001, "type")

    assert {item.metadata.severity for item in signatures} == frozenset(
        get_args(SeverityType)
    )
    assert {item.metadata.symptom for item in signatures} == frozenset(
        get_args(SymptomType)
    )

    for source_type in ("file", "sysfs"):
        assert {
            event.path["format"]
            for event in events
            if event.type == source_type
        } == FileAdapter.FORMATS
    assert {
        event.path["size"] for event in events if event.type == "i2c"
    } == {"b", "w", "l"}

    actions = _operations(signatures)
    queries = _operations(signatures, queries=True)
    action_types = {item.type for item in actions}
    query_types = {item.type for item in queries}
    assert BUILTIN_OPERATION_TYPES <= action_types
    assert action_types - BUILTIN_OPERATION_TYPES
    assert {"cli", "dse"} <= query_types
    assert query_types - {"cli", "dse"}
    _assert_operation_and_artifact_shapes(signatures, actions, queries)


def test_positive_corpus_exercises_logic_cadence_and_dse_composition():
    signatures = _wire_result().ruleset.signatures
    events = _events(signatures)
    expressions = [item.conditions.logic for item in signatures]

    assert any(
        "AND" in item and "OR" in item and "(" not in item
        for item in expressions
    )
    assert any("(" in item and ")" in item for item in expressions)
    assert any(_parenthesis_depth(item) > 1 for item in expressions)
    assert {item.conditions.logic_lookback_time == 0 for item in signatures} == {
        False,
        True,
    }
    assert any(item.match_count > 1 and item.match_period > 0 for item in events)
    assert {item.sampling_interval is None for item in events} == {False, True}
    assert {item.async_collection for item in events} == {False, True}
    assert any(
        event.type == "platform_api" and isinstance(event.path, str)
        for event in events
    )
    assert any(event.instances for event in events if event.type != "dse")
    assert any(event.type == "dse" and event.async_collection for event in events)
    assert any(
        item.type == "dse" and item.evaluation.type == "dse"
        for item in events
    )
    assert any(
        item.type == "dse" and item.evaluation.type != "dse"
        for item in events
    )
    assert any(
        item.type not in ("dse", "platform_api")
        and item.evaluation.type == "dse"
        for item in events
    )
    assert any(_has_cross_selector_dse(item) for item in signatures)
    assert any(
        item.evaluation.type == "dse" and item.evaluation.operator is not None
        for item in events
    )
    for operator in ("AND", "OR"):
        assert any(
            _has_mixed_multi_dse_logic(item, operator)
            for item in signatures
        )


def _has_cross_selector_dse(signature):
    for event in signature.conditions.events:
        if event.type != "dse" or event.evaluation.type != "dse":
            continue
        if (
            parse_reference(event.path).selector
            != parse_reference(event.evaluation.value).selector
        ):
            return True
    return False


def _has_mixed_multi_dse_logic(signature, operator):
    dse_count = sum(
        event.type == "dse" for event in signature.conditions.events
    )
    has_common = any(
        event.type != "dse" and not event.instances
        for event in signature.conditions.events
    )
    return dse_count > 1 and has_common and operator in signature.conditions.logic


def _parenthesis_depth(expression):
    depth = maximum = 0
    for character in expression:
        if character == "(":
            depth += 1
            maximum = max(maximum, depth)
        elif character == ")":
            depth -= 1
    return maximum


def test_dut_extension_corpus_is_wire_only_and_qualification_scoped():
    result = load_rules(DUT_EXTENSION_CORPUS, materialize=False)
    assert result.file_valid, result.file_errors
    assert not result.broken_rules, result.broken_rules
    assert result.ruleset is not None
    signatures = result.ruleset.signatures
    assert len(signatures) == 11

    ids = [item.metadata.id for item in signatures]
    names = [item.metadata.name for item in signatures]
    assert len(ids) == len(set(ids))
    assert len(names) == len(set(names))
    assert all(
        "dldd-dut-qualification" in item.metadata.tags
        for item in signatures
    )

    controls = [
        item for item in signatures if "extension-control" in item.metadata.tags
    ]
    assert len(controls) == 1
    assert not any(
        tag.startswith("broken-") for tag in controls[0].metadata.tags
    )

    failures = [item for item in signatures if item not in controls]
    broken_categories = [
        tuple(
            tag for tag in item.metadata.tags if tag.startswith("broken-")
        )
        for item in failures
    ]
    assert len(failures) == 10
    assert all(len(categories) == 1 for categories in broken_categories)
    assert len({categories[0] for categories in broken_categories}) == 10
