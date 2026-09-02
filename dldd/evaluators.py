"""Deterministic event evaluators."""

from __future__ import annotations

import operator
from types import MappingProxyType
from typing import Any, Callable, Mapping

import regex as bounded_regex


REGEX_TIMEOUT_SECONDS = 0.1


class EvaluationContractError(ValueError):
    pass


COMPARATORS: Mapping[str, Callable[[Any, Any], bool]] = MappingProxyType({
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    "equals": operator.eq,
    "not_equals": operator.ne,
})
COMPARISON_OPERATORS = frozenset(COMPARATORS)


def parse_integer(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if isinstance(value, str):
        return int(value.strip(), 0)
    raise EvaluationContractError("value is not an integer: {!r}".format(value))


def _coerce_pair(actual: Any, expected: Any) -> Any:
    if isinstance(expected, bool):
        if isinstance(actual, str):
            normalized = actual.strip().lower()
            if normalized in ("true", "1", "yes", "on"):
                actual = True
            elif normalized in ("false", "0", "no", "off"):
                actual = False
        return actual, expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        return parse_integer(actual), expected
    if isinstance(expected, float):
        return float(actual), expected
    return actual, expected


def evaluate(specification: Mapping[str, Any], actual: Any) -> bool:
    evaluator_type = specification.get("type")
    expected = specification.get("value")

    if evaluator_type == "mask":
        if specification.get("logic") != "&":
            raise EvaluationContractError("mask evaluation supports '&' only")
        mask = parse_integer(expected)
        return parse_integer(actual) & mask == mask

    if evaluator_type in ("comparison", "dse"):
        op = specification.get("operator")
        if evaluator_type == "dse" and not op:
            comparator = specification.get("comparator")
            if not callable(comparator):
                raise EvaluationContractError(
                    "DSE evaluation requires an operator or resolved comparator"
                )
            return bool(comparator(actual))
        if op not in COMPARATORS:
            label = "DSE" if evaluator_type == "dse" else "comparison"
            raise EvaluationContractError("unsupported {} operator: {}".format(label, op))
        left, right = _coerce_pair(actual, expected)
        return COMPARATORS[op](left, right)

    if evaluator_type == "string":
        op = specification.get("operator")
        left = str(actual)
        right = str(expected)
        if not specification.get("case_sensitive", True):
            left, right = left.casefold(), right.casefold()
        if op == "contains":
            return right in left
        if op == "equals":
            return left == right
        if op == "regex":
            try:
                return bounded_regex.search(
                    right,
                    left,
                    timeout=REGEX_TIMEOUT_SECONDS,
                ) is not None
            except TimeoutError:
                raise EvaluationContractError(
                    "regex evaluation exceeded {:.3f} seconds".format(
                        REGEX_TIMEOUT_SECONDS
                    )
                )
            except bounded_regex.error as error:
                raise EvaluationContractError("invalid regex: {}".format(error))
        raise EvaluationContractError("unsupported string operator: {}".format(op))

    if evaluator_type == "boolean":
        left, right = _coerce_pair(actual, expected)
        if not isinstance(right, bool) or not isinstance(left, bool):
            raise EvaluationContractError("boolean evaluation requires boolean values")
        return left is right

    raise EvaluationContractError(
        "unsupported evaluation type: {}".format(evaluator_type)
    )
