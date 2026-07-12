from __future__ import absolute_import

from dataclasses import replace

import pytest

from dldd.dse import (
    DSEBinding,
    DSEContext,
    DSEError,
    DSEEvaluationHandle,
    DSEExpansionPolicy,
    DSEExpansionResult,
    DSEHook,
    DSEReference,
    DSEReferenceError,
    DSERegistry,
    DSESourceHandle,
    DSEUnresolvedError,
    ResolvedCommand,
    ResolvedEvaluation,
    parse_reference,
    validate_resolved_evaluation,
)
from dldd.models import Operation, ResolvedSource, ValueConfig


REFERENCE = DSEReference("sensor*", "read")
CONTEXT = DSEContext(rule_name="RULE", event_id=1)


def _unsafe_value_config(**changes):
    config = object.__new__(ValueConfig)
    values = {
        "type": "float",
        "unit": "C",
        "scaling": "N/A",
        "encoding": "N/A",
    }
    values.update(changes)
    for name, value in values.items():
        object.__setattr__(config, name, value)
    return config


class ConfigurableHook(DSEHook):
    def __init__(self, source=None, evaluation=None, action=None, query=None):
        self.source = source
        self.evaluation = evaluation
        self.action = action
        self.query = query
        self.validated = []

    def resolve_source(self, reference, context):
        return self.source

    def resolve_evaluation(self, reference, context):
        return self.evaluation

    def resolve_action(self, command, context):
        if self.action is None:
            return super().resolve_action(command, context)
        return self.action

    def resolve_query(self, command, context):
        if self.query is None:
            return super().resolve_query(command, context)
        return self.query

    def validate_vendor_operation(self, operation, context):
        self.validated.append((operation, context))


@pytest.mark.parametrize(
    "value, expected",
    (
        pytest.param(DSEReference("sensor", "read"), "sensor:read()", id="scalar"),
        pytest.param(
            DSEReference("sensor*", "read"), "{sensor*}:{read()}", id="star"
        ),
        pytest.param(
            DSEReference("sensor?", "read"), "{sensor?}:{read()}", id="question"
        ),
        pytest.param(None, None, id="none"),
        pytest.param(1, None, id="integer"),
        pytest.param({}, None, id="mapping"),
        pytest.param([], None, id="sequence"),
    ),
)
def test_reference_contract_canonicalizes_patterns_and_rejects_non_strings(
    value, expected
):
    if expected is None:
        with pytest.raises(DSEReferenceError, match="must be a string"):
            parse_reference(value)
    else:
        assert value.canonical == expected


@pytest.mark.parametrize(
    "changes, error, message",
    (
        pytest.param(
            {"bootstrap_scans": 0}, ValueError, "bootstrap_scans", id="zero-scans"
        ),
        pytest.param(
            {"bootstrap_interval": 0},
            ValueError,
            "bootstrap_interval",
            id="zero-bootstrap-interval",
        ),
        pytest.param(
            {"warmup_cycles": 0}, ValueError, "warmup_cycles", id="zero-cycles"
        ),
        pytest.param(
            {"stable_interval": 0},
            ValueError,
            "stable_interval",
            id="zero-stable-interval",
        ),
        pytest.param(
            {"bootstrap_scans": True},
            TypeError,
            "positive integer",
            id="boolean-scans",
        ),
        pytest.param(
            {"bootstrap_scans": 1.5},
            TypeError,
            "positive integer",
            id="fractional-scans",
        ),
        pytest.param(
            {"warmup_cycles": False},
            TypeError,
            "positive integer",
            id="boolean-cycles",
        ),
        pytest.param(
            {"warmup_cycles": 2.5},
            TypeError,
            "positive integer",
            id="fractional-cycles",
        ),
        pytest.param(
            {"bootstrap_interval": True},
            TypeError,
            "finite positive number",
            id="boolean-bootstrap-interval",
        ),
        pytest.param(
            {"bootstrap_interval": "1"},
            TypeError,
            "finite positive number",
            id="string-bootstrap-interval",
        ),
        pytest.param(
            {"bootstrap_interval": float("nan")},
            TypeError,
            "finite positive number",
            id="nan-bootstrap-interval",
        ),
        pytest.param(
            {"stable_interval": False},
            TypeError,
            "finite positive number",
            id="boolean-stable-interval",
        ),
        pytest.param(
            {"stable_interval": float("inf")},
            TypeError,
            "finite positive number",
            id="infinite-stable-interval",
        ),
        pytest.param(
            {"stable_interval": float("-inf")},
            TypeError,
            "finite positive number",
            id="negative-infinite-stable-interval",
        ),
        pytest.param(
            {"bootstrap_interval": 0.05, "stable_interval": 0.1},
            None,
            None,
            id="positive-fractional-intervals",
        ),
    ),
)
def test_expansion_policy_enforces_scheduler_types_ranges_and_finite_intervals(
    changes, error, message
):
    if error is not None:
        with pytest.raises(error, match=message):
            DSEExpansionPolicy(**changes)
    else:
        policy = DSEExpansionPolicy(**changes)
        assert policy.bootstrap_interval == 0.05
        assert policy.stable_interval == 0.1


@pytest.mark.parametrize(
    "kind, payload, error, message",
    (
        pytest.param(
            "binding",
            {"instance": "", "source_id": "key"},
            ValueError,
            "instance",
            id="empty-instance",
        ),
        pytest.param(
            "binding",
            {"instance": 1, "source_id": "key"},
            ValueError,
            "instance",
            id="non-string-instance",
        ),
        pytest.param(
            "binding",
            {"instance": "sensor", "source_id": ""},
            ValueError,
            "source_id",
            id="empty-source-id",
        ),
        pytest.param(
            "binding",
            {"instance": "sensor", "source_id": 1},
            ValueError,
            "source_id",
            id="non-string-source-id",
        ),
        pytest.param(
            "binding",
            {
                "instance": "sensor",
                "source_id": "key",
                "value_configs": _unsafe_value_config(type="vendor-private"),
            },
            ValueError,
            "invalid DSE binding value_configs",
            id="invalid-value-config",
        ),
        pytest.param(
            "inventory",
            (object(),),
            TypeError,
            "DSEBinding objects",
            id="untyped-inventory",
        ),
        pytest.param(
            "inventory",
            (DSEBinding("sensor", "key"),) * 2,
            ValueError,
            "must be unique",
            id="duplicate-inventory",
        ),
        pytest.param("authority", 0, TypeError, "must be a bool", id="integer-zero"),
        pytest.param("authority", 1, TypeError, "must be a bool", id="integer-one"),
        pytest.param("authority", None, TypeError, "must be a bool", id="none"),
        pytest.param(
            "authority", "true", TypeError, "must be a bool", id="string"
        ),
        pytest.param("valid", None, None, None, id="valid-frozen-result"),
    ),
)
def test_expansion_data_contracts_validate_bindings_inventory_and_authority(
    kind, payload, error, message
):
    if kind == "binding":
        factory = lambda: DSEBinding(**payload)
    elif kind == "inventory":
        factory = lambda: DSEExpansionResult(payload)
    elif kind == "authority":
        factory = lambda: DSEExpansionResult((), authoritative=payload)
    else:
        binding = DSEBinding("sensor", "key")
        result = DSEExpansionResult([binding], authoritative=True)
        assert result.bindings == (binding,)
        assert result.authoritative is True
        return

    with pytest.raises(error, match=message):
        factory()


@pytest.mark.parametrize(
    "factory, error, message",
    (
        pytest.param(
            lambda: DSESourceHandle(
                "sensor:read()", lambda context: None, lambda invocation: None
            ),
            TypeError,
            "DSEReference",
            id="invalid-source-reference-type",
        ),
        pytest.param(
            lambda: DSESourceHandle(REFERENCE, None, lambda invocation: None),
            TypeError,
            "functions must be callable",
            id="missing-expand-callback",
        ),
        pytest.param(
            lambda: DSESourceHandle(REFERENCE, lambda context: None, None),
            TypeError,
            "functions must be callable",
            id="missing-get-value-callback",
        ),
        pytest.param(
            lambda: DSESourceHandle(
                REFERENCE,
                lambda context: None,
                lambda invocation: None,
                policy=object(),
            ),
            TypeError,
            "DSEExpansionPolicy",
            id="invalid-expansion-policy",
        ),
        pytest.param(
            lambda: DSEEvaluationHandle(
                "sensor:threshold()", lambda invocation: None
            ),
            TypeError,
            "DSEReference",
            id="invalid-evaluation-reference-type",
        ),
        pytest.param(
            lambda: DSEEvaluationHandle(REFERENCE, None),
            TypeError,
            "must be callable",
            id="missing-comparator-callback",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                object(), rule_operator=None, reference=REFERENCE
            ),
            DSEError,
            "must return ResolvedEvaluation",
            id="non-resolved-evaluation",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(comparator="not-callable"),
                rule_operator=None,
                reference=REFERENCE,
            ),
            DSEError,
            "comparator must be callable",
            id="non-callable-comparator",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(expected_value=1, operator="approximately"),
                rule_operator=None,
                reference=REFERENCE,
            ),
            DSEError,
            "unsupported operator",
            id="unsupported-resolved-operator",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(expected_value=True, operator=">"),
                rule_operator=None,
                reference=REFERENCE,
            ),
            DSEError,
            "numeric or string expected value",
            id="boolean-ordered-comparison",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(expected_value=None),
                rule_operator="==",
                reference=REFERENCE,
            ),
            DSEError,
            "requires a resolved expected value",
            id="rule-operator-missing-value",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(expected_value=1),
                rule_operator=None,
                reference=REFERENCE,
            ),
            DSEError,
            "does not provide comparator semantics",
            id="missing-comparator-semantics",
        ),
        pytest.param(
            lambda: validate_resolved_evaluation(
                ResolvedEvaluation(
                    expected_value=1,
                    operator="==",
                    value_configs=_unsafe_value_config(unit=""),
                ),
                rule_operator=None,
                reference=REFERENCE,
            ),
            DSEError,
            "unit must be a non-empty string",
            id="invalid-resolved-value-config",
        ),
    ),
)
def test_runtime_handles_and_evaluations_reject_unexecutable_typed_contracts(
    factory, error, message
):
    with pytest.raises(error, match=message):
        factory()


def test_unavailable_dse_and_optional_operations_fail_closed():
    hook = ConfigurableHook(source=(), evaluation=ResolvedEvaluation())

    with pytest.raises(DSEUnresolvedError, match="action .* not exposed"):
        hook.resolve_action(REFERENCE, CONTEXT)
    with pytest.raises(DSEUnresolvedError, match="query .* not exposed"):
        hook.resolve_query("opaque command", CONTEXT)
    assert hook.validate_resolved_source(object(), CONTEXT) is None

    registry = DSERegistry()
    assert registry.hook is None

    with pytest.raises(DSEUnresolvedError, match="no vendor DSE hook"):
        registry.resolve_source("sensor:read()", CONTEXT)


@pytest.mark.parametrize(
    "source, message",
    (
        (object(), "or a sequence"),
        ("not-a-sequence-contract", "or a sequence"),
        ((), "resolved to no operations"),
        ((object(),), "expected ResolvedSource"),
        ((ResolvedSource(type="", path={}),), "type must be a non-empty"),
        (
            (ResolvedSource(type="redis", path={}, instance=1),),
            "instance must be a non-empty",
        ),
        (
            (
                ResolvedSource(
                    type="redis",
                    path={},
                    instance="sensor",
                    value_configs=_unsafe_value_config(encoding=""),
                ),
            ),
            "invalid DSE resolved source value_configs",
        ),
        (
            (ResolvedSource(type="redis", path={}),),
            "wildcard DSE source .* identify each component instance",
        ),
    ),
)
def test_registry_rejects_invalid_direct_source_contracts(source, message):
    registry = DSERegistry(hook=ConfigurableHook(source=source))

    with pytest.raises((DSEError, DSEUnresolvedError), match=message):
        registry.resolve_source("{sensor*}:{read()}", CONTEXT)


def test_registry_typed_source_handle_command_and_operation_contracts():
    """Validate typed results, deferred commands, and advertised operations."""

    source = ResolvedSource(
        type="redis", path={"key": "SENSOR|0"}, instance="sensor0"
    )
    registry = DSERegistry(hook=ConfigurableHook(source=[source]))

    assert registry.resolve_source("{sensor*}:{read()}", CONTEXT) == (source,)

    other = DSEReference("other*", "read")
    source_handle = DSESourceHandle(
        other, lambda context: DSEExpansionResult(()), lambda invocation: None
    )
    evaluation_handle = DSEEvaluationHandle(
        other, lambda invocation: ResolvedEvaluation(expected_value=1, operator=">=")
    )

    with pytest.raises(DSEError, match="source handle reference"):
        DSERegistry(hook=ConfigurableHook(source=source_handle)).resolve_source(
            "{sensor*}:{read()}", CONTEXT
        )
    with pytest.raises(DSEError, match="evaluation handle reference"):
        DSERegistry(
            hook=ConfigurableHook(evaluation=evaluation_handle)
        ).resolve_evaluation("{sensor*}:{read()}", CONTEXT)

    # Deferred commands and advertised operations remain typed and validated.
    invalid_commands = (
        ("action", object(), "resolver must return ResolvedCommand"),
        ("query", object(), "resolver must return ResolvedCommand"),
        ("action", ResolvedCommand(executor=None), "executor must be callable"),
        ("query", ResolvedCommand(executor=None), "executor must be callable"),
    )
    for kind, resolved, message in invalid_commands:
        hook = ConfigurableHook(**{kind: resolved})
        registry = DSERegistry(hook=hook)
        with pytest.raises(DSEError, match=message):
            getattr(registry, "resolve_{}".format(kind))(
                "opaque command", CONTEXT
            )

    hook = ConfigurableHook()
    registry = DSERegistry(hook=hook, action_types=("platform-reset",))
    operation = Operation(type="platform-reset")

    registry.validate_vendor_operation(operation, CONTEXT)
    assert hook.validated == [(operation, CONTEXT)]

    with pytest.raises(DSEError, match="is not advertised"):
        registry.validate_vendor_operation(
            replace(operation, type="unadvertised"), CONTEXT
        )
