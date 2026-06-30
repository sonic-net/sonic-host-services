from __future__ import absolute_import

import json
from pathlib import Path

import pytest

from dldd.dse import (
    DSEContext,
    DSEHook,
    DSEReference,
    DSEReferenceError,
    DSERegistry,
    ResolvedCommand,
    ResolvedEvaluation,
)
from dldd.validation import ValidationContext, validate_document


FIXTURE = Path(__file__).parent / "fixtures" / "valid-redis-rule.json"


class RecordingHook(DSEHook):
    def __init__(self):
        self.action_command = None
        self.query_command = None

    def resolve_source(self, reference, context):
        return ()

    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(comparator=lambda value: bool(value))

    def resolve_action(self, command, context):
        self.action_command = command
        return ResolvedCommand(executor=lambda operation: None)

    def resolve_query(self, command, context):
        self.query_command = command
        return ResolvedCommand(executor=lambda operation: None)


def test_canonical_action_and_query_commands_remain_typed_references():
    hook = RecordingHook()
    registry = DSERegistry(hook=hook)

    registry.resolve_action("PSU:reset()", DSEContext())
    registry.resolve_query("{psu*}:{get_status()}", DSEContext())

    assert hook.action_command == DSEReference(selector="PSU", function="reset")
    assert hook.query_command == DSEReference(
        selector="psu*", function="get_status"
    )


def test_opaque_action_and_query_commands_reach_vendor_hook_unchanged():
    hook = RecordingHook()
    registry = DSERegistry(hook=hook)
    action = "reset power rail 7"
    query = "collect-blackbox --scope psu0"

    registry.resolve_action(action, DSEContext())
    registry.resolve_query(query, DSEContext())

    assert hook.action_command is action
    assert hook.query_command is query


def test_opaque_commands_materialize_through_the_rules_contract():
    with FIXTURE.open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    actions = document["signatures"][0]["signature"]["actions"]
    local = actions["repair_actions"]["local_actions"]
    local["action_list"] = [
        {
            "action": {
                "type": "dse",
                "command": "reset power rail 7",
                "timeout": 10,
            }
        }
    ]
    actions["log_collection"]["queries"] = [
        {"query": {"type": "dse", "command": "collect blackbox for psu0"}}
    ]
    hook = RecordingHook()

    result = validate_document(
        document,
        context=ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )

    assert result.activation_valid
    assert hook.action_command == "reset power rail 7"
    assert hook.query_command == "collect blackbox for psu0"
    materialized_actions = result.materialized_rules[0].signature.actions
    assert callable(
        materialized_actions.repair_actions.local_actions.action_list[0].executor
    )
    assert callable(materialized_actions.log_collection.queries[0].executor)


@pytest.mark.parametrize("method", ("resolve_action", "resolve_query"))
@pytest.mark.parametrize("command", (None, 42, b"PSU:reset()", ""))
def test_dse_operation_commands_must_be_non_empty_strings(method, command):
    registry = DSERegistry(hook=RecordingHook())

    with pytest.raises(DSEReferenceError, match="must (?:be a string|not be empty)"):
        getattr(registry, method)(command, DSEContext())
