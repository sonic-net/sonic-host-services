"""Contract checks for the checked-in exhaustive DLDD rule corpus.

The corpus is operator-facing example data, not another schema authority.  The
tests therefore derive finite wire values from the installed version-owned
Pydantic models and describe behavioral coverage with stable tags.  Adding a
new example must not require updating a signature count.
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
    load_document,
    load_rules,
)


FIXTURES = Path(__file__).parent / "fixtures"
POSITIVE_CORPUS = FIXTURES / "all-supported-rule-types.yaml"
BROKEN_CORPUS = FIXTURES / "localized-broken-rule-types.yaml"
DUT_EXTENSION_CORPUS = FIXTURES / "dut-unsupported-extension-rules.yaml"

REQUIRED_COVERAGE_TAGS = frozenset(
    (
        "coverage-source-types",
        "coverage-evaluation-types",
        "coverage-comparison-operators",
        "coverage-string-operators",
        "coverage-value-config-types",
        "coverage-file-formats",
        "coverage-sysfs-formats",
        "coverage-i2c-variants",
        "coverage-cli-variants",
        "coverage-platform-api-variants",
        "coverage-logic",
        "logic-and",
        "logic-or",
        "logic-precedence",
        "logic-parentheses",
        "lookback-current",
        "lookback-windowed",
        "match-window",
        "cadence-inherited",
        "cadence-explicit",
        "async",
        "coverage-dse",
        "dse-runtime-pair",
        "dse-source-static-eval",
        "dse-direct-source-runtime-eval",
        "dse-direct-result",
        "dse-fixed-eval",
        "dse-trusted-comparator",
        "dse-cross-selector",
        "dse-common-predicate",
        "dse-rule-operator",
        "dse-common-and",
        "dse-common-or",
        "dse-multi-and",
        "dse-multi-or",
        "logic-nested-parentheses",
        "coverage-actions",
        "coverage-queries",
        "coverage-logs",
        "coverage-metadata",
    )
)

BROKEN_MATERIALIZATION_EXPECTATIONS = {
    "DLDD_BROKEN_MISSING_SEVERITY": (
        "coverage-broken-missing-required",
        "missing_field",
    ),
    "DLDD_BROKEN_EVENT_TYPE": (
        "coverage-broken-event-type",
        "unsupported_type",
    ),
    "DLDD_BROKEN_EVALUATION_TYPE": (
        "coverage-broken-evaluation-type",
        "unsupported_type",
    ),
    "DLDD_BROKEN_COMPARISON_OPERATOR": (
        "coverage-broken-comparison-operator",
        "unsupported_value",
    ),
    "DLDD_BROKEN_DSE_REFERENCE": (
        "coverage-broken-dse-reference",
        "invalid_format",
    ),
    "DLDD_BROKEN_DSE_UNRESOLVED": (
        "coverage-broken-dse-unresolved",
        "materialization_failed",
    ),
    "DLDD_BROKEN_LOGIC_SYNTAX": (
        "coverage-broken-logic-syntax",
        "invalid_logic",
    ),
    "DLDD_BROKEN_LOGIC_ID_ZERO": (
        "coverage-broken-logic-id",
        "invalid_logic",
    ),
    "DLDD_BROKEN_LOGIC_UNDEFINED": (
        "coverage-broken-logic-undefined",
        "invalid_logic",
    ),
    "DLDD_BROKEN_DUPLICATE_EVENT_ID": (
        "coverage-broken-logic-duplicate",
        "duplicate_event_id",
    ),
    "DLDD_BROKEN_MATCH_WINDOW": (
        "coverage-broken-match-window",
        "invalid_match_window",
    ),
    "DLDD_BROKEN_POSITIONAL_PATH": (
        "coverage-broken-positional-path",
        "instance_path_mismatch",
    ),
    "DLDD_BROKEN_POSITIONAL_VALUE": (
        "coverage-broken-positional-value",
        "instance_value_mismatch",
    ),
    "DLDD_BROKEN_DUPLICATE_INSTANCE": (
        "coverage-broken-duplicate-instance",
        "duplicate_instance",
    ),
    "DLDD_BROKEN_ACTION_TIMEOUT": (
        "coverage-broken-action-timeout",
        "missing_action_timeout",
    ),
    "DLDD_BROKEN_I2C_SET_VALUE": (
        "coverage-broken-i2c-set-value",
        "missing_i2c_value",
    ),
    "DLDD_BROKEN_VENDOR_ACTION": (
        "coverage-broken-vendor-action",
        "materialization_failed",
    ),
    "DLDD_BROKEN_VENDOR_QUERY": (
        "coverage-broken-vendor-query",
        "materialization_failed",
    ),
}

BROKEN_PREFLIGHT_EXPECTATIONS = {
    "DLDD_BROKEN_FILE_FORMAT": "coverage-broken-file-format",
    "DLDD_BROKEN_PLATFORM_HOOK": "coverage-broken-platform-hook",
}


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


def _corpus_tags(signatures):
    return frozenset(
        tag for signature in signatures for tag in signature.metadata.tags
    )


def _tagged(signatures, tag):
    return tuple(
        signature for signature in signatures if tag in signature.metadata.tags
    )


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
    materialized = validation.materialized_rules

    direct_rules = {
        item.signature.metadata.id
        for item in materialized
        if "dse-direct-result" in item.signature.metadata.tags
    }
    assert direct_rules
    assert any(
        event.sources and event.dse_source_handle is None
        for item in materialized
        if item.signature.metadata.id in direct_rules
        for event in item.events
    )
    assert any(item.rule_id in direct_rules for item in plans.work_items.values())
    assert not any(
        template.item.rule_id in direct_rules
        for template in plans.templates.values()
    )

    fixed_rules = {
        item.signature.metadata.id
        for item in materialized
        if "dse-fixed-eval" in item.signature.metadata.tags
    }
    assert fixed_rules
    assert any(
        event.event.evaluation.type == "dse"
        and event.dse_evaluation_handle is None
        for item in materialized
        if item.signature.metadata.id in fixed_rules
        for event in item.events
    )
    assert any(
        item.rule_id in fixed_rules
        and item.dse_evaluation_handle is None
        and item.evaluation["value"] == 10
        for item in plans.work_items.values()
    )

    trusted_rules = {
        item.signature.metadata.id
        for item in materialized
        if "dse-trusted-comparator" in item.signature.metadata.tags
    }
    assert trusted_rules
    trusted_handles = [
        event.dse_evaluation_handle
        for item in materialized
        if item.signature.metadata.id in trusted_rules
        for event in item.events
        if event.dse_evaluation_handle is not None
    ]
    assert trusted_handles
    resolved = trusted_handles[0].get_comparator(
        DSEInvocationContext(
            rule=next(
                event.dse_context
                for item in materialized
                if item.signature.metadata.id in trusted_rules
                for event in item.events
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

    assert "direct" in hook.source_modes.values()
    assert "runtime" in hook.source_modes.values()
    assert {"fixed", "trusted-comparator", "runtime"} <= set(
        hook.evaluation_modes.values()
    )
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


def test_positive_corpus_is_valid_tagged_and_materializable():
    wire = _wire_result()
    signatures = wire.ruleset.signatures
    names = {signature.metadata.name for signature in signatures}

    assert names
    assert all("dldd-corpus" in item.metadata.tags for item in signatures)
    missing_tags = REQUIRED_COVERAGE_TAGS - _corpus_tags(signatures)
    assert not missing_tags, "missing corpus coverage tags: {}".format(
        sorted(missing_tags)
    )

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


def test_positive_corpus_names_logic_cadence_and_dse_edge_semantics():
    signatures = _wire_result().ruleset.signatures
    events = _events(signatures)

    # Tags are stable case names; predicates prove the tagged example still
    # demonstrates that behavior rather than becoming a decorative label.
    semantic_cases = {
        "logic-and": lambda signature: "AND" in signature.conditions.logic,
        "logic-or": lambda signature: "OR" in signature.conditions.logic,
        "logic-precedence": lambda signature: (
            "AND" in signature.conditions.logic
            and "OR" in signature.conditions.logic
            and "(" not in signature.conditions.logic
        ),
        "logic-parentheses": lambda signature: (
            "(" in signature.conditions.logic
            and ")" in signature.conditions.logic
        ),
        "logic-nested-parentheses": lambda signature: (
            _parenthesis_depth(signature.conditions.logic) > 1
        ),
        "lookback-current": lambda signature: (
            signature.conditions.logic_lookback_time == 0
        ),
        "lookback-windowed": lambda signature: (
            signature.conditions.logic_lookback_time > 0
        ),
        "match-window": lambda signature: any(
            event.match_count > 1 and event.match_period > 0
            for event in signature.conditions.events
        ),
        "cadence-inherited": lambda signature: any(
            event.sampling_interval is None
            for event in signature.conditions.events
        ),
        "cadence-explicit": lambda signature: any(
            event.sampling_interval is not None
            for event in signature.conditions.events
        ),
        "async": lambda signature: any(
            event.async_collection for event in signature.conditions.events
        ),
        "dse-runtime-pair": lambda signature: any(
            event.type == "dse" and event.evaluation.type == "dse"
            for event in signature.conditions.events
        ),
        "dse-source-static-eval": lambda signature: any(
            event.type == "dse" and event.evaluation.type != "dse"
            for event in signature.conditions.events
        ),
        "dse-direct-source-runtime-eval": lambda signature: any(
            event.type not in ("dse", "platform_api")
            and event.evaluation.type == "dse"
            for event in signature.conditions.events
        ),
        "dse-cross-selector": _has_cross_selector_dse,
        "dse-common-predicate": _has_dse_common_predicate,
        "dse-common-and": lambda signature: (
            _has_dse_common_with_logic(signature, "AND")
        ),
        "dse-common-or": lambda signature: (
            _has_dse_common_with_logic(signature, "OR")
        ),
        "dse-multi-and": lambda signature: (
            _has_multiple_dse_with_logic(signature, "AND")
        ),
        "dse-multi-or": lambda signature: (
            _has_multiple_dse_with_logic(signature, "OR")
        ),
        "dse-rule-operator": lambda signature: any(
            event.evaluation.type == "dse"
            and event.evaluation.operator is not None
            for event in signature.conditions.events
        ),
    }
    for tag, predicate in semantic_cases.items():
        tagged = _tagged(signatures, tag)
        assert tagged, "corpus has no case tagged {!r}".format(tag)
        assert any(predicate(signature) for signature in tagged), tag

    assert any(
        event.type == "platform_api" and isinstance(event.path, str)
        for event in events
    )
    assert any(event.instances for event in events if event.type != "dse")
    assert any(event.type == "dse" and event.async_collection for event in events)


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


def _has_dse_common_predicate(signature):
    has_dse = any(
        event.type == "dse" for event in signature.conditions.events
    )
    has_common = any(
        event.type != "dse" and not event.instances
        for event in signature.conditions.events
    )
    return has_dse and has_common and "AND" in signature.conditions.logic


def _has_dse_common_with_logic(signature, operator):
    has_dse = any(
        event.type == "dse" for event in signature.conditions.events
    )
    has_common = any(
        event.type != "dse" and not event.instances
        for event in signature.conditions.events
    )
    return has_dse and has_common and operator in signature.conditions.logic


def _has_multiple_dse_with_logic(signature, operator):
    dse_events = sum(
        event.type == "dse" for event in signature.conditions.events
    )
    return dse_events > 1 and operator in signature.conditions.logic


def _parenthesis_depth(expression):
    depth = maximum = 0
    for character in expression:
        if character == "(":
            depth += 1
            maximum = max(maximum, depth)
        elif character == ")":
            depth -= 1
    return maximum


def test_localized_broken_corpus_localizes_every_named_signature():
    document = load_document(BROKEN_CORPUS)
    raw_by_name = {
        item["signature"]["metadata"]["name"]: item["signature"]
        for item in document["signatures"]
    }
    identity = PlatformIdentity(
        platform="corpus",
        product_id="8102_28fh_dpu_o",
        software_version="grboudre_dldd-impl.0-1d85491a7",
    )
    registry = DSERegistry()
    result = load_rules(
        BROKEN_CORPUS,
        context=ValidationContext(
            product_id=identity.product_id,
            software_version=identity.software_version,
            require_compatibility_identity=True,
            dse_registry=registry,
        ),
    )

    assert result.file_valid, result.file_errors
    assert result.ruleset is not None
    broken_by_name = {item.rule_name: item for item in result.broken_rules}
    assert set(broken_by_name) == set(BROKEN_MATERIALIZATION_EXPECTATIONS)
    for name, (tag, code) in BROKEN_MATERIALIZATION_EXPECTATIONS.items():
        assert tag in raw_by_name[name]["metadata"]["tags"]
        assert {issue.code for issue in broken_by_name[name].issues} == {code}

    survivor_names = {
        item.signature.metadata.name for item in result.materialized_rules
    }
    assert survivor_names == set(BROKEN_PREFLIGHT_EXPECTATIONS)
    for name, tag in BROKEN_PREFLIGHT_EXPECTATIONS.items():
        assert tag in raw_by_name[name]["metadata"]["tags"]

    extensions = PlatformExtensions(
        identity=identity,
        dse_registry=registry,
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=ExactCompatibilityMatcher(),
    )
    preflight = preflight_activation(
        result,
        extensions,
        {"redis": 1, "file": 1, "common": 1},
    )
    assert {
        item.rule_name: item.code for item in preflight.failures
    } == {
        name: "activation_preflight_failed"
        for name in BROKEN_PREFLIGHT_EXPECTATIONS
    }


def test_dut_extension_corpus_is_wire_only_and_qualification_scoped():
    document = load_document(DUT_EXTENSION_CORPUS)
    assert len(document["signatures"]) == 11

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
