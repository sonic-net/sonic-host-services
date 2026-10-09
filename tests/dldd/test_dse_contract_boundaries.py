from __future__ import absolute_import

from dataclasses import replace

import pytest

from dldd.dse import (
    DSEBinding,
    DSEContext,
    DSEError,
    DSEEvaluationHandle,
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
from dldd.models import Operation, ResolvedSource


REFERENCE = DSEReference("sensor*", "read")
CONTEXT = DSEContext(rule_name="RULE", event_id=1)


class ConfigurableHook(DSEHook):
    def __init__(self, source=None, evaluation=None, action=None):
        self.source = source
        self.evaluation = evaluation
        self.action = action
        self.validated = []

    def resolve_source(self, reference, context):
        return self.source

    def resolve_evaluation(self, reference, context):
        return self.evaluation

    def resolve_action(self, command, context):
        if self.action is None:
            return super().resolve_action(command, context)
        return self.action

    def validate_vendor_operation(self, operation, context):
        self.validated.append((operation, context))


def test_reference_and_binding_value_contracts():
    assert DSEReference("sensor", "read").canonical == "sensor:read()"
    assert REFERENCE.canonical == "{sensor*}:{read()}"
    with pytest.raises(DSEReferenceError, match="must be a string"):
        parse_reference(None)

def test_expansion_inventory_identity_contract():
    binding = DSEBinding("sensor", "key")
    result = DSEExpansionResult([binding])
    assert result.bindings == (binding,)

    with pytest.raises(ValueError, match="instance"):
        DSEBinding("", "key")
    with pytest.raises(ValueError, match="must be unique"):
        DSEExpansionResult((binding, binding))


def test_runtime_handles_require_executable_typed_contracts():
    source = DSESourceHandle(
        REFERENCE,
        lambda context: DSEExpansionResult(()),
        lambda invocation: None,
    )
    evaluation = DSEEvaluationHandle(
        REFERENCE,
        lambda invocation: ResolvedEvaluation(expected_value=1, operator=">="),
    )
    assert source.reference is REFERENCE
    assert evaluation.reference is REFERENCE

    with pytest.raises(TypeError, match="functions must be callable"):
        DSESourceHandle(REFERENCE, None, lambda invocation: None)
    with pytest.raises(DSEError, match="comparator must be callable"):
        validate_resolved_evaluation(
            ResolvedEvaluation(comparator="not-callable"),
            rule_operator=None,
            reference=REFERENCE,
        )


def test_unavailable_dse_fails_closed():
    hook = ConfigurableHook(source=(), evaluation=ResolvedEvaluation())
    with pytest.raises(DSEUnresolvedError, match="action .* not exposed"):
        hook.resolve_action(REFERENCE, CONTEXT)

    registry = DSERegistry()
    assert registry.hook is None
    with pytest.raises(DSEUnresolvedError, match="no vendor DSE hook"):
        registry.resolve_source("sensor:read()", CONTEXT)


def test_registry_source_command_and_vendor_operation_contracts():
    source = ResolvedSource(
        type="redis", path={"key": "SENSOR|0"}, instance="sensor0"
    )
    registry = DSERegistry(hook=ConfigurableHook(source=[source]))
    assert registry.resolve_source("{sensor*}:{read()}", CONTEXT) == (source,)

    source_handle = DSESourceHandle(
        REFERENCE,
        lambda context: DSEExpansionResult(()),
        lambda invocation: None,
    )
    registry = DSERegistry(hook=ConfigurableHook(source=source_handle))
    assert registry.resolve_source("{sensor*}:{read()}", CONTEXT) is source_handle

    other_reference = DSEReference("other*", "read")
    mismatched_source = DSESourceHandle(
        other_reference,
        lambda context: DSEExpansionResult(()),
        lambda invocation: None,
    )
    with pytest.raises(DSEError, match="source handle reference"):
        DSERegistry(
            hook=ConfigurableHook(source=mismatched_source)
        ).resolve_source("{sensor*}:{read()}", CONTEXT)

    mismatched_evaluation = DSEEvaluationHandle(
        other_reference,
        lambda invocation: ResolvedEvaluation(comparator=lambda value: True),
    )
    with pytest.raises(DSEError, match="evaluation handle reference"):
        DSERegistry(
            hook=ConfigurableHook(evaluation=mismatched_evaluation)
        ).resolve_evaluation("{sensor*}:{read()}", CONTEXT)

    registry = DSERegistry(
        hook=ConfigurableHook(
            source=(ResolvedSource(type="redis", path={}),)
        )
    )
    with pytest.raises(DSEError, match="identify each component instance"):
        registry.resolve_source("{sensor*}:{read()}", CONTEXT)

    registry = DSERegistry(
        hook=ConfigurableHook(
            action=ResolvedCommand(executor=lambda operation: None)
        ),
        action_types=("platform-reset",),
    )
    assert callable(registry.resolve_action("opaque command", CONTEXT).executor)

    operation = Operation(type="platform-reset")
    registry.validate_vendor_operation(operation, CONTEXT)
    assert registry.hook.validated == [(operation, CONTEXT)]
    with pytest.raises(DSEError, match="is not advertised"):
        registry.validate_vendor_operation(
            replace(operation, type="unadvertised"), CONTEXT
        )

    bad_registry = DSERegistry(
        hook=ConfigurableHook(action=ResolvedCommand(executor=None))
    )
    with pytest.raises(DSEError, match="executor must be callable"):
        bad_registry.resolve_action("opaque command", CONTEXT)
