"""Parser and evaluator for DLDD signature Boolean expressions.

The initial rule contract intentionally has a very small language: positive integer event
IDs, ``AND``, ``OR``, and parentheses.  Keeping the parser here avoids using
Python expression evaluation for vendor supplied input.
"""

from __future__ import absolute_import

from dataclasses import dataclass
import re


class LogicSyntaxError(ValueError):
    """Raised when a conditions.logic expression is not valid."""


@dataclass(frozen=True)
class EventReference(object):
    event_id: int


@dataclass(frozen=True)
class AndExpression(object):
    left: object
    right: object


@dataclass(frozen=True)
class OrExpression(object):
    left: object
    right: object


_TOKEN = re.compile(r"\s*(?:(AND|OR)|([0-9]+)|(\()|(\))|(\S+))")
MAX_LOGIC_CHARACTERS = 16384
MAX_LOGIC_TOKENS = 4096
MAX_LOGIC_NESTING = 64
MAX_LOGIC_EVENT_ID = 999


def _tokenize(expression):
    if not isinstance(expression, str) or not expression.strip():
        raise LogicSyntaxError("logic expression must be a non-empty string")
    expression = expression.strip()
    if len(expression) > MAX_LOGIC_CHARACTERS:
        raise LogicSyntaxError(
            "logic expression exceeds {} characters".format(
                MAX_LOGIC_CHARACTERS
            )
        )

    tokens = []
    position = 0
    nesting = 0
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        operator, number, left, right, invalid = match.groups()
        if invalid is not None:
            raise LogicSyntaxError(
                "unsupported token {!r} at character {}".format(invalid, position)
            )
        if operator is not None:
            tokens.append((operator, position))
        elif number is not None:
            significant = number.lstrip("0") or "0"
            if len(significant) > len(str(MAX_LOGIC_EVENT_ID)):
                raise LogicSyntaxError(
                    "event ID is too large at character {}".format(position)
                )
            event_id = int(significant)
            tokens.append((event_id, position))
        elif left is not None:
            nesting += 1
            if nesting > MAX_LOGIC_NESTING:
                raise LogicSyntaxError(
                    "logic nesting exceeds {} levels".format(
                        MAX_LOGIC_NESTING
                    )
                )
            tokens.append(("(", position))
        else:
            nesting -= 1
            tokens.append((")", position))
        if len(tokens) > MAX_LOGIC_TOKENS:
            raise LogicSyntaxError(
                "logic expression exceeds {} tokens".format(
                    MAX_LOGIC_TOKENS
                )
            )
        position = match.end()

    return tokens


class _Parser(object):
    def __init__(self, tokens):
        self._tokens = tokens
        self._offset = 0

    def parse(self):
        result = self._parse_or()
        if self._offset != len(self._tokens):
            value, position = self._tokens[self._offset]
            raise LogicSyntaxError(
                "unexpected token {!r} at character {}".format(value, position)
            )
        return result

    def _peek(self):
        if self._offset == len(self._tokens):
            return None
        return self._tokens[self._offset][0]

    def _consume(self):
        token = self._tokens[self._offset]
        self._offset += 1
        return token

    def _parse_or(self):
        # AND has the conventional higher precedence.  Parentheses remain the
        # preferred form for rules where author intent might otherwise be hard
        # to read.
        result = self._parse_and()
        while self._peek() == "OR":
            self._consume()
            result = OrExpression(result, self._parse_and())
        return result

    def _parse_and(self):
        result = self._parse_primary()
        while self._peek() == "AND":
            self._consume()
            result = AndExpression(result, self._parse_primary())
        return result

    def _parse_primary(self):
        token = self._peek()
        if isinstance(token, int):
            self._consume()
            if token == 0:
                raise LogicSyntaxError("event IDs in logic expressions start at 1")
            return EventReference(token)
        if token == "(":
            self._consume()
            result = self._parse_or()
            if self._peek() != ")":
                raise LogicSyntaxError("missing closing parenthesis")
            self._consume()
            return result
        if token is None:
            raise LogicSyntaxError("unexpected end of logic expression")
        value, position = self._tokens[self._offset]
        raise LogicSyntaxError(
            "expected event ID or '(' at character {}, got {!r}".format(
                position, value
            )
        )


def parse_logic(expression, valid_event_ids=None):
    """Parse *expression* and optionally verify all referenced event IDs."""

    tree = _Parser(_tokenize(expression)).parse()
    if valid_event_ids is not None:
        valid = set(valid_event_ids)
        unknown = sorted(collect_event_ids(tree) - valid)
        if unknown:
            raise LogicSyntaxError(
                "logic references undefined event IDs: {}".format(
                    ", ".join(str(value) for value in unknown)
                )
            )
    return tree


def collect_event_ids(expression):
    """Return the event IDs referenced by a parsed expression."""

    event_ids = set()
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, EventReference):
            event_ids.add(node.event_id)
        elif isinstance(node, (AndExpression, OrExpression)):
            pending.extend((node.left, node.right))
        else:
            raise TypeError(
                "unsupported logic expression node {!r}".format(node)
            )
    return event_ids


def evaluate_logic(expression, event_states):
    """Evaluate a parsed expression against ``{event_id: truth_value}``.

    Missing IDs are false.  Callers that need missing references to be errors
    should validate with :func:`parse_logic` before runtime evaluation.
    """

    pending = [(expression, False)]
    values = []
    while pending:
        node, combine = pending.pop()
        if isinstance(node, EventReference):
            values.append(bool(event_states.get(node.event_id, False)))
        elif isinstance(node, (AndExpression, OrExpression)):
            if combine:
                right = values.pop()
                left = values.pop()
                values.append(
                    left and right
                    if isinstance(node, AndExpression)
                    else left or right
                )
            else:
                pending.extend(
                    ((node, True), (node.right, False), (node.left, False))
                )
        else:
            raise TypeError(
                "unsupported logic expression node {!r}".format(node)
            )
    return values[0]
