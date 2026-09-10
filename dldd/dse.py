"""Typed, vendor-extensible Data Source Extension contracts.

The daemon receives a hook object from platform integration code.  Rules never
name Python modules, classes, or expressions to execute; they can only select
references or opaque commands exposed by that already-installed hook.
"""

from __future__ import absolute_import

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
import re
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .models import (
    Operation,
    ResolvedSource,
    ValueConfig,
    frozen_mapping,
    value_config_contract_errors,
)
from .evaluators import COMPARISON_OPERATORS


class DSEError(ValueError):
    """Base error for malformed or unresolved DSE contracts."""


class DSEReferenceError(DSEError):
    pass


class DSEUnresolvedError(DSEError):
    pass


def _require_value_configs(value_configs, label, error_type):
    errors = value_config_contract_errors(value_configs)
    if errors:
        raise error_type(
            "invalid {} value_configs: {}".format(label, "; ".join(errors))
        )


@dataclass(frozen=True)
class DSEReference(object):
    selector: str
    function: str

    @property
    def canonical(self):
        if "*" in self.selector or "?" in self.selector:
            return "{{{}}}:{{{}()}}".format(self.selector, self.function)
        return "{}:{}()".format(self.selector, self.function)


@dataclass(frozen=True)
class DSEContext(object):
    product_id: Optional[str] = None
    software_version: Optional[str] = None
    component: Optional[str] = None
    rule_name: Optional[str] = None
    event_id: Optional[int] = None


@dataclass(frozen=True)
class DSEBinding(object):
    """One runtime instance returned by a DSE source expander."""

    instance: str
    source_id: str
    data: Mapping[str, Any] = field(default_factory=frozen_mapping)
    value_configs: ValueConfig = field(default_factory=ValueConfig)

    def __post_init__(self):
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("DSE binding instance must be a non-empty string")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("DSE binding source_id must be a non-empty string")
        object.__setattr__(self, "data", frozen_mapping(self.data))
        _require_value_configs(self.value_configs, "DSE binding", ValueError)


@dataclass(frozen=True)
class DSEExpansionResult(object):
    """Runtime source expansion returned by a trusted vendor handle."""

    bindings: Tuple[DSEBinding, ...]

    def __post_init__(self):
        bindings = tuple(self.bindings)
        if any(not isinstance(item, DSEBinding) for item in bindings):
            raise TypeError("DSE expansion bindings must be DSEBinding objects")
        identities = [(item.instance, item.source_id) for item in bindings]
        if len(identities) != len(set(identities)):
            raise ValueError("DSE expansion bindings must be unique")
        object.__setattr__(self, "bindings", bindings)


@dataclass(frozen=True)
class DSEInvocationContext(object):
    """Runtime-only context passed to vendor collection/evaluation handles."""

    rule: DSEContext
    binding: DSEBinding


@dataclass(frozen=True)
class DSESourceHandle(object):
    """Resolved source functions retained without invocation in the plan."""

    reference: DSEReference
    expand: Callable[[DSEContext], DSEExpansionResult]
    get_value: Callable[[DSEInvocationContext], Any]

    def __post_init__(self):
        if not isinstance(self.reference, DSEReference):
            raise TypeError("DSE source handle requires a DSEReference")
        if not callable(self.expand) or not callable(self.get_value):
            raise TypeError("DSE source handle functions must be callable")


@dataclass(frozen=True)
class DSEEvaluationHandle(object):
    """Resolved comparator function retained for monitor-time invocation."""

    reference: DSEReference
    get_comparator: Callable[[DSEInvocationContext], "ResolvedEvaluation"]

    def __post_init__(self):
        if not isinstance(self.reference, DSEReference):
            raise TypeError("DSE evaluation handle requires a DSEReference")
        if not callable(self.get_comparator):
            raise TypeError("DSE comparator handle function must be callable")


@dataclass(frozen=True)
class ResolvedEvaluation(object):
    """A resolved expected value or a complete vendor comparator."""

    expected_value: Any = None
    operator: Optional[str] = None
    comparator: Optional[Callable[[Any], bool]] = None
    value_configs: ValueConfig = field(default_factory=ValueConfig)

    @property
    def complete(self):
        return self.comparator is not None or self.operator is not None


_ORDERING_OPERATORS = frozenset((">", "<", ">=", "<="))


def validate_resolved_evaluation(
    resolved, rule_operator=None, reference=None
):
    """Validate one typed activation- or monitor-time DSE comparator result."""

    if not isinstance(resolved, ResolvedEvaluation):
        raise DSEError("DSE comparator must return ResolvedEvaluation")
    _require_value_configs(
        resolved.value_configs, "DSE resolved evaluation", DSEError
    )
    if resolved.comparator is not None and not callable(resolved.comparator):
        raise DSEError("DSE evaluation comparator must be callable")
    if (
        resolved.operator is not None
        and resolved.operator not in COMPARISON_OPERATORS
    ):
        raise DSEError(
            "DSE evaluation resolver returned unsupported operator {!r}".format(
                resolved.operator
            )
        )
    effective_operator = rule_operator or resolved.operator
    if effective_operator is not None and resolved.expected_value is None:
        raise DSEError(
            "DSE operator {!r} requires a resolved expected value".format(
                effective_operator
            )
        )
    if effective_operator in _ORDERING_OPERATORS and (
        not isinstance(
            resolved.expected_value, (int, float, str)
        )
        or isinstance(resolved.expected_value, bool)
    ):
        raise DSEError(
            "DSE ordering operator requires a numeric or string expected value"
        )
    if rule_operator is None and not resolved.complete:
        label = (
            reference.canonical
            if isinstance(reference, DSEReference)
            else str(reference or "")
        )
        raise DSEError(
            "DSE evaluation {} does not provide comparator semantics".format(
                label
            )
        )
    return resolved


@dataclass(frozen=True)
class ResolvedCommand(object):
    """Runtime command whose executor accepts one immutable Operation."""

    executor: Callable[[Operation], Any]
    vendor_data: Mapping[str, Any] = field(default_factory=frozen_mapping)

    def __post_init__(self):
        object.__setattr__(self, "vendor_data", frozen_mapping(self.vendor_data))


_REFERENCE = re.compile(
    r"^(?:\{(?P<braced_selector>[A-Za-z0-9_.?*\-]+)\}|"
    r"(?P<selector>[A-Za-z0-9_.\-]+)):"
    r"(?:\{(?P<braced_function>[A-Za-z_][A-Za-z0-9_]*\(\))\}|"
    r"(?P<function>[A-Za-z_][A-Za-z0-9_]*\(\)))$"
)


def parse_reference(value):
    if not isinstance(value, str):
        raise DSEReferenceError("DSE reference must be a string")
    match = _REFERENCE.match(value)
    if match is None:
        raise DSEReferenceError(
            "invalid DSE reference; expected '<selector>:<function>()' or "
            "'{<selector>}:{<function>()}'"
        )
    selector = match.group("braced_selector") or match.group("selector")
    function = match.group("braced_function") or match.group("function")
    if bool(match.group("braced_selector")) != bool(
        match.group("braced_function")
    ):
        raise DSEReferenceError(
            "invalid DSE reference; selector and function must use matching braces"
        )
    return DSEReference(selector=selector, function=function[:-2])


def _operation_command(value):
    """Parse a DSE reference or preserve an opaque vendor command."""

    if not isinstance(value, str):
        raise DSEReferenceError("DSE operation command must be a string")
    if not value:
        raise DSEReferenceError("DSE operation command must not be empty")
    try:
        return parse_reference(value)
    except DSEReferenceError:
        return value


def _operation_label(command):
    return (
        command.canonical
        if isinstance(command, DSEReference)
        else str(command)
    )


class DSEHook(object, metaclass=ABCMeta):
    """Interface implemented by trusted platform/vendor packages."""

    @abstractmethod
    def resolve_source(self, reference, context) -> DSESourceHandle:
        """Return configured sources without reading their values."""

    @abstractmethod
    def resolve_evaluation(self, reference, context) -> DSEEvaluationHandle:
        """Return a configured evaluator without sampling source values."""

    def resolve_action(self, command, context) -> ResolvedCommand:
        """Resolve an action contract without executing the action."""

        raise DSEUnresolvedError(
            "DSE action {} is not exposed".format(_operation_label(command))
        )

    def resolve_query(self, command, context) -> ResolvedCommand:
        """Resolve a query contract without executing the query."""

        raise DSEUnresolvedError(
            "DSE query {} is not exposed".format(_operation_label(command))
        )

    def validate_vendor_operation(self, operation, context):
        """Validate an operation without executing it or reading its target."""

        return None

    def validate_resolved_source(self, source, context):
        """Validate a resolved source binding without sampling its value."""

        return None


class DSERegistry(object):
    """Explicit registry for the installed vendor hook and advertised types."""

    def __init__(
        self,
        hook=None,
        source_types=(),
        action_types=(),
        query_types=(),
    ):
        self._hook = hook
        self.source_types = frozenset(source_types)
        self.action_types = frozenset(action_types)
        self.query_types = frozenset(query_types)

    @property
    def hook(self):
        return self._hook

    def _require_hook(self):
        if self._hook is None:
            raise DSEUnresolvedError("no vendor DSE hook is installed")
        return self._hook

    def resolve_source(self, value, context):
        reference = parse_reference(value)
        sources = self._require_hook().resolve_source(reference, context)
        if isinstance(sources, DSESourceHandle):
            if sources.reference != reference:
                raise DSEError(
                    "DSE source handle reference does not match requested reference"
                )
            return sources
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            raise DSEError(
                "DSE source resolver must return DSESourceHandle or a sequence"
            )
        resolved = tuple(sources)
        if not resolved:
            raise DSEUnresolvedError(
                "DSE source {} resolved to no operations".format(reference.canonical)
            )
        for source in resolved:
            if not isinstance(source, ResolvedSource):
                raise DSEError(
                    "DSE source resolver returned {!r}, expected ResolvedSource".format(
                        type(source).__name__
                    )
                )
            if not isinstance(source.type, str) or not source.type:
                raise DSEError("DSE resolved source type must be a non-empty string")
            if source.instance is not None and (
                not isinstance(source.instance, str) or not source.instance
            ):
                raise DSEError(
                    "DSE resolved source instance must be a non-empty string"
                )
            _require_value_configs(
                source.value_configs, "DSE resolved source", DSEError
            )
            if (
                ("*" in reference.selector or "?" in reference.selector)
                and not source.instance
            ):
                raise DSEError(
                    "wildcard DSE source {} must identify each component instance".format(
                        reference.canonical
                    )
                )
        return resolved

    def resolve_evaluation(self, value, context, rule_operator=None):
        reference = parse_reference(value)
        resolved = self._require_hook().resolve_evaluation(reference, context)
        if isinstance(resolved, DSEEvaluationHandle):
            if resolved.reference != reference:
                raise DSEError(
                    "DSE evaluation handle reference does not match requested reference"
                )
            return resolved
        return validate_resolved_evaluation(
            resolved,
            rule_operator=rule_operator,
            reference=reference,
        )

    def resolve_action(self, value, context):
        """Resolve one advertised action without executing it."""

        return self._resolve_operation(value, context, "action")

    def resolve_query(self, value, context):
        """Resolve one advertised artifact query without executing it."""

        return self._resolve_operation(value, context, "query")

    def _resolve_operation(self, value, context, kind):
        """Apply the shared typed command contract to an action or query."""

        command = _operation_command(value)
        resolver = getattr(self._require_hook(), "resolve_{}".format(kind))
        resolved = resolver(command, context)
        if not isinstance(resolved, ResolvedCommand):
            raise DSEError(
                "DSE {} resolver must return ResolvedCommand".format(kind)
            )
        if not callable(resolved.executor):
            raise DSEError("DSE {} executor must be callable".format(kind))
        return resolved

    def validate_vendor_operation(self, operation, context, query=False):
        advertised = self.query_types if query else self.action_types
        if operation.type not in advertised:
            raise DSEError(
                "vendor operation type {!r} is not advertised".format(operation.type)
            )
        self._require_hook().validate_vendor_operation(operation, context)


EMPTY_DSE_REGISTRY = DSERegistry()
