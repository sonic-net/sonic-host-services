from __future__ import absolute_import

import pytest

from dldd.logic import (
    MAX_LOGIC_NESTING,
    AndExpression,
    EventReference,
    LogicSyntaxError,
    OrExpression,
    collect_event_ids,
    evaluate_logic,
    parse_logic,
)


def test_single_event_expression():
    expression = parse_logic("  1  ", {1})

    assert expression == EventReference(1)
    assert evaluate_logic(expression, {1: True}) is True
    assert evaluate_logic(expression, {}) is False


def test_and_has_higher_precedence_than_or():
    expression = parse_logic("1 OR 2 AND 3", {1, 2, 3})

    assert expression == OrExpression(
        EventReference(1), AndExpression(EventReference(2), EventReference(3))
    )
    assert evaluate_logic(expression, {1: False, 2: True, 3: True}) is True
    assert evaluate_logic(expression, {1: False, 2: True, 3: False}) is False


def test_parentheses_override_precedence_and_ids_are_collected():
    expression = parse_logic("(1 OR 2) AND 3", {1, 2, 3})

    assert collect_event_ids(expression) == {1, 2, 3}
    assert evaluate_logic(expression, {1: True, 2: False, 3: True}) is True
    assert evaluate_logic(expression, {1: True, 2: False, 3: False}) is False


def test_large_flat_expression_uses_iterative_traversal_and_evaluation():
    event_ids = set(range(1, 1000))
    expression = parse_logic(
        " AND ".join(str(value) for value in sorted(event_ids)), event_ids
    )

    assert collect_event_ids(expression) == event_ids
    assert evaluate_logic(
        expression, {value: True for value in event_ids}
    ) is True
    assert evaluate_logic(
        expression, {value: value != 500 for value in event_ids}
    ) is False


def test_excessive_parenthesis_nesting_is_rejected_cleanly():
    source = "(" * (MAX_LOGIC_NESTING + 1) + "1" + ")" * (
        MAX_LOGIC_NESTING + 1
    )

    with pytest.raises(LogicSyntaxError, match="nesting exceeds"):
        parse_logic(source)


@pytest.mark.parametrize(
    "expression",
    (
        "",
        "1 and 2",
        "1 NOT 2",
        "0",
        "1 AND",
        "AND 1",
        "(1 OR 2",
        "1 2",
        "1 OR ()",
    ),
)
def test_invalid_expressions_are_rejected(expression):
    with pytest.raises(LogicSyntaxError):
        parse_logic(expression)


def test_unknown_event_reference_is_rejected():
    with pytest.raises(LogicSyntaxError, match="undefined event IDs: 3"):
        parse_logic("1 AND 3", {1, 2})
