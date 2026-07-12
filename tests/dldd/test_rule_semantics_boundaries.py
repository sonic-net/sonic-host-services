from __future__ import absolute_import

from dataclasses import replace
from types import SimpleNamespace

import pytest

from dldd import evaluators as dldd_evaluators
from dldd.evaluators import (
    EvaluationContractError,
    evaluate,
    parse_integer,
)
from dldd.hooks import VendorHook, VendorHookError, VendorHookRegistry
from dldd.logic import (
    MAX_LOGIC_CHARACTERS,
    MAX_LOGIC_TOKENS,
    EventReference,
    LogicSyntaxError,
    collect_event_ids,
    evaluate_logic,
    parse_logic,
)
from dldd.models import ResolvedSource, ValueConfig
from dldd.planner import (
    _canonical_binding_value,
    _component_name,
    _evaluation_mapping,
    _source_mapping,
    build_plans,
)
from dldd.timestamps import floor_timestamp_fields
from dldd.validation import load_rules


def test_integer_builtin_dse_and_regex_evaluator_contracts(monkeypatch):
    """Execute supported wire values and contain invalid evaluator behavior."""

    for value, expected in (
        (True, 1),
        (False, 0),
        (7, 7),
        (b"0x10", 16),
        (" 0b101 ", 5),
        (1.5, None),
        (object(), None),
        (None, None),
    ):
        if expected is None:
            with pytest.raises(EvaluationContractError, match="not an integer"):
                parse_integer(value)
        else:
            assert parse_integer(value) == expected

    # Built-in and DSE evaluator happy paths and contract errors.
    assert evaluate(
        {"type": "comparison", "operator": ">", "value": 1.5},
        "2.0",
    )
    assert evaluate(
        {"type": "comparison", "operator": "==", "value": True},
        "yes",
    )
    assert evaluate(
        {"type": "comparison", "operator": "==", "value": False},
        "off",
    )
    assert evaluate(
        {"type": "comparison", "operator": "==", "value": True},
        True,
    )
    assert evaluate(
        {"type": "comparison", "operator": "==", "value": "sensor"},
        "sensor",
    )
    assert evaluate(
        {"type": "string", "operator": "equals", "value": "sensor"},
        "sensor",
    )
    assert evaluate(
        {"type": "dse", "comparator": lambda actual: actual == "fault"},
        "fault",
    )
    assert evaluate(
        {"type": "dse", "operator": ">=", "value": 10},
        "12",
    )

    with pytest.raises(EvaluationContractError, match="supports '&' only"):
        evaluate({"type": "mask", "logic": "|", "value": 1}, 1)
    with pytest.raises(EvaluationContractError, match="boolean values"):
        evaluate({"type": "boolean", "value": True}, "maybe")
    with pytest.raises(EvaluationContractError, match="string operator"):
        evaluate({"type": "string", "operator": "starts_with", "value": "S"}, "S0")

    with pytest.raises(EvaluationContractError, match="resolved comparator"):
        evaluate({"type": "dse", "comparator": "not-callable"}, 1)
    with pytest.raises(EvaluationContractError, match="DSE operator"):
        evaluate({"type": "dse", "operator": "approximately", "value": 1}, 1)
    with pytest.raises(EvaluationContractError, match="evaluation type"):
        evaluate({"type": "vendor-expression", "value": 1}, 1)

    # Regex compilation and execution errors remain bounded.
    with pytest.raises(EvaluationContractError, match="invalid regex"):
        evaluate(
            {"type": "string", "operator": "regex", "value": "["},
            "sensor",
        )

    monkeypatch.setattr(
        dldd_evaluators.bounded_regex,
        "search",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(EvaluationContractError, match="exceeded"):
        evaluate(
            {"type": "string", "operator": "regex", "value": ".*"},
            "sensor",
        )


class DefaultHook(VendorHook):
    def __init__(self):
        self.collected = []

    def collect(self, operation):
        self.collected.append(operation)
        return {"value": 7}

    def execute_action(self, action):
        return {"state": "done"}


class DelegatingHook(VendorHook):
    def collect(self, operation):
        return super().collect(operation)

    def execute_action(self, action):
        return super().execute_action(action)


def test_vendor_hook_registry_and_i2c_resolution_contract():
    """Enforce typed hooks, unique registration, and complete I2C resolution."""

    hook = DefaultHook()
    query = {"operation": "diagnostics"}

    assert hook.validate_source(query) is None
    assert hook.collect_query(query) == {"value": 7}
    assert hook.resolve_i2c_bus("logical-6", query) == "logical-6"
    assert hook.collected == [query]

    with pytest.raises(NotImplementedError):
        DelegatingHook().collect({})
    with pytest.raises(NotImplementedError):
        DelegatingHook().execute_action({})

    registry = VendorHookRegistry()
    registered = DefaultHook()

    for name, candidate in (("", registered), ("sensor", object())):
        with pytest.raises(ValueError, match="name and VendorHook"):
            registry.register(name, candidate)

    registry.register("sensor", registered)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("sensor", registered)
    with pytest.raises(VendorHookError, match="not registered"):
        registry.get("missing")
    assert registry.get_optional("missing") is None

    # Invalid vendor bus results fail the typed contract.
    for resolved in (None, "", True, 1.5, object()):
        class InvalidI2CHook(DefaultHook):
            def resolve_i2c_bus(self, bus, operation):
                return resolved

        registry = VendorHookRegistry()
        registry.register("i2c", InvalidI2CHook())
        with pytest.raises(VendorHookError, match="non-empty string or integer"):
            registry.resolve_i2c_bus("logical", {"bus": "logical"})

    # Validation resolves every configured positional bus.
    observed = []

    class I2CHook(DefaultHook):
        def validate_source(self, operation):
            observed.append(("validate", operation["bus"]))

        def resolve_i2c_bus(self, bus, operation):
            observed.append(("resolve", bus))
            return int(bus)

    registry = VendorHookRegistry()
    registry.register("i2c", I2CHook())
    operation = {"bus": ["6", "7"]}

    registry.validate_i2c_source(operation)

    assert observed == [
        ("validate", ["6", "7"]),
        ("resolve", "6"),
        ("resolve", "7"),
    ]


def test_logic_parser_enforces_limits_tokens_and_tree_contracts():
    with pytest.raises(LogicSyntaxError, match="characters"):
        parse_logic("1" * (MAX_LOGIC_CHARACTERS + 1))

    token_heavy = " ".join("1" for unused in range(MAX_LOGIC_TOKENS + 1))
    with pytest.raises(LogicSyntaxError, match="tokens"):
        parse_logic(token_heavy)

    with pytest.raises(LogicSyntaxError, match="unsupported token"):
        parse_logic("1 @ 2")
    with pytest.raises(LogicSyntaxError, match="start at 1"):
        parse_logic("0")
    with pytest.raises(TypeError, match="unsupported logic expression"):
        collect_event_ids(object())
    with pytest.raises(TypeError, match="unsupported logic expression"):
        evaluate_logic(object(), {})

    event_id = int("9" * 1000)

    assert parse_logic(str(event_id)) == EventReference(event_id)


def test_planner_canonicalizes_bindings_evaluations_and_value_config():
    assert _component_name(None, "SENSOR") == "SENSOR"
    assert _component_name("SENSOR0:path", "SENSOR") == "SENSOR0"

    platform = ResolvedSource(type="platform_api", path="read_temperature")
    literal = ResolvedSource(type="vendor", path=7, vendor_data={"unit": "C"})
    assert _source_mapping(platform) == {
        "hook": "platform",
        "operation": "read_temperature",
    }
    assert _source_mapping(literal) == {"value": 7, "unit": "C"}

    def callback():
        return None

    canonical = _canonical_binding_value(
        {
            "bytes": b"\x00\xff",
            "values": (None, True, 7, 1.5, "text"),
            "callback": callback,
            "opaque": object(),
        }
    )
    assert canonical["bytes"] == {"bytes_hex": "00ff"}
    assert canonical["values"] == [None, True, 7, 1.5, "text"]
    assert canonical["callback"]["callable"].endswith(".callback")
    assert canonical["opaque"]["object_type"] == "builtins.object"

    comparator = lambda actual: bool(actual)
    event = SimpleNamespace(
        evaluation=SimpleNamespace(
            type="mask",
            value="0x80",
            value_configs=ValueConfig(type="int", unit="bits"),
            operator=None,
            logic="&",
            unit="bits",
            comparator=comparator,
            case_sensitive=True,
        )
    )

    mapping = _evaluation_mapping(event, 0)

    assert mapping["logic"] == "&"
    assert mapping["unit"] == "bits"
    assert mapping["comparator"] is comparator
    assert "operator" not in mapping

    validation = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    materialized = validation.materialized_rules[0]
    source = ResolvedSource(
        type="redis",
        path={
            "database": "STATE_DB",
            "table": "TEMPERATURE_INFO",
            "key": "TEMPERATURE_INFO|SENSOR0",
            "path": "temperature",
        },
        instance="SENSOR0",
        vendor_data={"scaling": 0.001, "unit": "C"},
    )
    event = replace(
        materialized.events[0].event,
        evaluation=replace(
            materialized.events[0].event.evaluation,
            value_configs=ValueConfig(),
        ),
    )
    materialized_event = replace(
        materialized.events[0], event=event, sources=(source,)
    )
    materialized = replace(materialized, events=(materialized_event,))

    bundle = build_plans(
        (materialized,),
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    item = next(iter(bundle.work_items.values()))

    assert item.value_config == ValueConfig(
        type="float",
        unit="C",
        scaling=0.001,
        encoding="N/A",
    )


def test_timestamp_flooring_preserves_tuple_shape_recursively():
    value = (
        {"observed_at": 10.9, "duration": 1.25},
        [{"hold-deadline": 20.8}],
    )

    assert floor_timestamp_fields(value) == (
        {"observed_at": 10, "duration": 1.25},
        [{"hold-deadline": 20}],
    )
