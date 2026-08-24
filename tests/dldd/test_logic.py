from __future__ import absolute_import

import pytest

from dldd.logic import (
    MAX_LOGIC_EVENT_ID,
    MAX_LOGIC_NESTING,
    AndExpression,
    EventReference,
    LogicSyntaxError,
    OrExpression,
    collect_event_ids,
    evaluate_logic,
    parse_logic,
)


def test_logic_parsing_and_evaluation_contract():
    expression = parse_logic("  1  ", {1})

    assert expression == EventReference(1)
    assert evaluate_logic(expression, {1: True}) is True
    assert evaluate_logic(expression, {}) is False

    expression = parse_logic("1 OR 2 AND 3", {1, 2, 3})

    assert expression == OrExpression(
        EventReference(1), AndExpression(EventReference(2), EventReference(3))
    )
    assert evaluate_logic(expression, {1: False, 2: True, 3: True}) is True
    assert evaluate_logic(expression, {1: False, 2: True, 3: False}) is False

    expression = parse_logic("(1 OR 2) AND 3", {1, 2, 3})

    assert collect_event_ids(expression) == {1, 2, 3}
    assert evaluate_logic(expression, {1: True, 2: False, 3: True}) is True
    assert evaluate_logic(expression, {1: True, 2: False, 3: False}) is False

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


def test_logic_rejects_invalid_contracts():
    source = "(" * (MAX_LOGIC_NESTING + 1) + "1" + ")" * (
        MAX_LOGIC_NESTING + 1
    )

    with pytest.raises(LogicSyntaxError, match="nesting exceeds"):
        parse_logic(source)

    for expression in (
        "",
        "1 and 2",
        "1 NOT 2",
        "0",
        "1 AND",
        "AND 1",
        "(1 OR 2",
        "1 2",
        "1 OR ()",
    ):
        with pytest.raises(LogicSyntaxError):
            parse_logic(expression)

    with pytest.raises(LogicSyntaxError, match="undefined event IDs: 3"):
        parse_logic("1 AND 3", {1, 2})

    assert parse_logic(str(MAX_LOGIC_EVENT_ID)) == EventReference(
        MAX_LOGIC_EVENT_ID
    )
    assert parse_logic("0001") == EventReference(1)
    with pytest.raises(LogicSyntaxError, match="event ID is too large"):
        parse_logic(str(MAX_LOGIC_EVENT_ID + 1))
    with pytest.raises(LogicSyntaxError, match="event ID is too large"):
        parse_logic("9" * 5000)
