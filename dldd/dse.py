"""Typed, vendor-extensible Data Source Extension contracts.

The daemon receives a hook object from platform integration code.  Rules never
name Python modules, classes, or expressions to execute; they can only select
references or opaque commands exposed by that already-installed hook.
"""

from __future__ import absolute_import

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .models import (
    ResolvedSource,
    ValueConfig,
    frozen_mapping,
    value_config_contract_errors,
)


class DSEError(ValueError):
    """Base error for malformed or unresolved DSE contracts."""


class DSEReferenceError(DSEError):
    pass


class DSEUnresolvedError(DSEError):
    pass


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
class ResolvedEvaluation(object):
    """A resolved expected value or a complete vendor comparator."""

    expected_value: Any = None
    operator: Optional[str] = None
    comparator: Optional[Callable[[Any], bool]] = None
    value_configs: ValueConfig = field(default_factory=ValueConfig)

    @property
    def complete(self):
        return self.comparator is not None or self.operator is not None


@dataclass(frozen=True)
class ResolvedCommand(object):
    """Opaque typed binding returned for DSE actions and queries."""

    executor: Callable[..., Any]
    vendor_data: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

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
    """Return a typed reference or an opaque vendor action/query command.

    Source and evaluation values always use :func:`parse_reference`.  The
    schema intentionally gives action/query ``command`` a wider contract: a
    canonical DSE reference is parsed for backward compatibility, while any
    other non-empty string is passed unchanged to the trusted vendor hook.
    """

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
    def resolve_source(self, reference, context):
        """Return a sequence of :class:`ResolvedSource` objects."""

    @abstractmethod
    def resolve_evaluation(self, reference, context):
        """Return a :class:`ResolvedEvaluation`."""

    def resolve_action(self, command, context):
        """Resolve a parsed :class:`DSEReference` or opaque command string."""

        raise DSEUnresolvedError(
            "DSE action {} is not exposed".format(_operation_label(command))
        )

    def resolve_query(self, command, context):
        """Resolve a parsed :class:`DSEReference` or opaque command string."""

        raise DSEUnresolvedError(
            "DSE query {} is not exposed".format(_operation_label(command))
        )

    def validate_vendor_operation(self, operation, context):
        """Validate a platform-advertised action/query operation.

        Vendors may raise :class:`DSEError` (or ``ValueError``) with a useful
        diagnostic.  Returning normally means the contract is supported.
        """

        return None

    def validate_vendor_source(self, event, context):
        """Validate an event using a platform-advertised source type."""

        return None

    def validate_resolved_source(self, source, context):
        """Validate a vendor-specific source produced by DSE resolution."""

        return None


class DSERegistry(object):
    """Explicit registry for the installed vendor hook and advertised types."""

    def __init__(
        self,
        hook=None,
        source_types=(),
        action_types=(),
        query_types=(),
        allow_unadvertised_operations=False,
    ):
        self._hook = hook
        self.source_types = frozenset(source_types)
        self.action_types = frozenset(action_types)
        self.query_types = frozenset(query_types)
        self.allow_unadvertised_operations = allow_unadvertised_operations

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
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            raise DSEError("DSE source resolver must return a sequence")
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
            value_config_errors = value_config_contract_errors(
                source.value_configs
            )
            if value_config_errors:
                raise DSEError(
                    "invalid DSE resolved source value_configs: {}".format(
                        "; ".join(value_config_errors)
                    )
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
        if not isinstance(resolved, ResolvedEvaluation):
            raise DSEError(
                "DSE evaluation resolver must return ResolvedEvaluation"
            )
        value_config_errors = value_config_contract_errors(
            resolved.value_configs
        )
        if value_config_errors:
            raise DSEError(
                "invalid DSE resolved evaluation value_configs: {}".format(
                    "; ".join(value_config_errors)
                )
            )
        if resolved.comparator is not None and not callable(resolved.comparator):
            raise DSEError("DSE evaluation comparator must be callable")
        if resolved.operator is not None and resolved.operator not in (
            ">", "<", ">=", "<=", "==", "!=", "equals", "not_equals"
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
        if effective_operator in (">", "<", ">=", "<="):
            if not isinstance(resolved.expected_value, (int, float, str)) or isinstance(
                resolved.expected_value, bool
            ):
                raise DSEError(
                    "DSE ordering operator requires a numeric or string expected value"
                )
        if rule_operator is None and not resolved.complete:
            raise DSEError(
                "DSE evaluation {} does not provide comparator semantics".format(
                    reference.canonical
                )
            )
        return resolved

    def resolve_action(self, value, context):
        command = _operation_command(value)
        resolved = self._require_hook().resolve_action(command, context)
        if not isinstance(resolved, ResolvedCommand):
            raise DSEError("DSE action resolver must return ResolvedCommand")
        if not callable(resolved.executor):
            raise DSEError("DSE action executor must be callable")
        return resolved

    def resolve_query(self, value, context):
        command = _operation_command(value)
        resolved = self._require_hook().resolve_query(command, context)
        if not isinstance(resolved, ResolvedCommand):
            raise DSEError("DSE query resolver must return ResolvedCommand")
        if not callable(resolved.executor):
            raise DSEError("DSE query executor must be callable")
        return resolved

    def validate_vendor_operation(self, operation, context, query=False):
        advertised = self.query_types if query else self.action_types
        if operation.type not in advertised:
            raise DSEError(
                "vendor operation type {!r} is not advertised".format(operation.type)
            )
        self._require_hook().validate_vendor_operation(operation, context)


EMPTY_DSE_REGISTRY = DSERegistry()
