"""Immutable rule models shared by DLDD validation and runtime code."""

from __future__ import absolute_import

from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple, Union


VALUE_CONFIG_TYPES = frozenset(
    ("binary", "hex", "int", "float", "string", "boolean", "json", "bytes", "N/A")
)


def frozen_mapping(value):
    """Return a recursively immutable, shallowly typed representation."""

    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return MappingProxyType(
        {key: freeze_value(item) for key, item in value.items()}
    )


def freeze_value(value):
    if isinstance(value, Mapping):
        return frozen_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    return value


def to_mutable(value):
    """Convert a rule model or frozen value to plain dict/list primitives."""

    if is_dataclass(value):
        return {
            item.name: to_mutable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: to_mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_mutable(item) for item in value]
    return value


@dataclass(frozen=True)
class ValueConfig(object):
    type: str = "N/A"
    unit: str = "N/A"
    scaling: Union[int, float, str] = "N/A"
    encoding: str = "N/A"


def value_config_contract_errors(config):
    """Return canonical contract errors for typed rule or DSE value metadata."""

    if not isinstance(config, ValueConfig):
        return ("must be ValueConfig",)
    errors = []
    if config.type not in VALUE_CONFIG_TYPES:
        errors.append("type must use a canonical value")
    if not isinstance(config.unit, str) or not config.unit:
        errors.append("unit must be a non-empty string")
    if not (
        (isinstance(config.scaling, (int, float)) and not isinstance(config.scaling, bool))
        or config.scaling == "N/A"
    ):
        errors.append("scaling must be numeric or 'N/A'")
    if not isinstance(config.encoding, str) or not config.encoding:
        errors.append("encoding must be a non-empty string")
    return tuple(errors)


@dataclass(frozen=True)
class Evaluation(object):
    type: str
    value: Any
    operator: Optional[str] = None
    logic: Optional[str] = None
    unit: Optional[str] = None
    case_sensitive: bool = True
    value_configs: ValueConfig = field(default_factory=ValueConfig)
    comparator: Optional[Callable[[Any], bool]] = None

    def __post_init__(self):
        object.__setattr__(self, "value", freeze_value(self.value))


@dataclass(frozen=True)
class Event(object):
    id: int
    type: str
    path: Any
    evaluation: Evaluation
    match_count: int
    match_period: int
    instances: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "path", freeze_value(self.path))
        object.__setattr__(self, "instances", tuple(self.instances))

    @property
    def source_type(self):
        return self.type


@dataclass(frozen=True)
class Conditions(object):
    logic: str
    logic_tree: object
    logic_lookback_time: int
    events: Tuple[Event, ...]

    def __post_init__(self):
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True)
class Metadata(object):
    name: str
    id: int
    version: str
    description: str
    product_ids: Tuple[str, ...]
    sw_versions: Tuple[str, ...]
    component: str
    symptom: str
    error_type: str
    severity: str
    priority: int = 5
    tags: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "product_ids", tuple(self.product_ids))
        object.__setattr__(self, "sw_versions", tuple(self.sw_versions))
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True)
class Operation(object):
    type: str
    command: Optional[str] = None
    argv: Tuple[str, ...] = ()
    path: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    timeout: Optional[int] = None
    max_output_bytes: Optional[int] = None
    executor: Optional[Callable[..., Any]] = None
    options: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self):
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "path", frozen_mapping(self.path))
        object.__setattr__(self, "options", frozen_mapping(self.options))


@dataclass(frozen=True)
class LocalActions(object):
    wait_period: int
    action_list: Tuple[Operation, ...]

    def __post_init__(self):
        object.__setattr__(self, "action_list", tuple(self.action_list))


@dataclass(frozen=True)
class RemoteActions(object):
    action_list: Tuple[str, ...]
    time_window: int

    def __post_init__(self):
        object.__setattr__(self, "action_list", tuple(self.action_list))


@dataclass(frozen=True)
class RepairActions(object):
    remote_actions: RemoteActions
    local_actions: Optional[LocalActions] = None


@dataclass(frozen=True)
class LogCollection(object):
    logs: Tuple[str, ...] = ()
    queries: Tuple[Operation, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "logs", tuple(self.logs))
        object.__setattr__(self, "queries", tuple(self.queries))


@dataclass(frozen=True)
class Actions(object):
    repair_actions: RepairActions
    log_collection: Optional[LogCollection] = None


@dataclass(frozen=True)
class Signature(object):
    metadata: Metadata
    conditions: Conditions
    actions: Actions


@dataclass(frozen=True)
class RuleSet(object):
    schema_version: str
    signatures: Tuple[Signature, ...]
    local_action_default_timeout: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(self, "signatures", tuple(self.signatures))


@dataclass(frozen=True)
class ResolvedSource(object):
    type: str
    path: Any
    instance: Optional[str] = None
    value_configs: ValueConfig = field(default_factory=ValueConfig)
    vendor_data: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self):
        object.__setattr__(self, "path", freeze_value(self.path))
        object.__setattr__(self, "vendor_data", frozen_mapping(self.vendor_data))


@dataclass(frozen=True)
class MaterializedEvent(object):
    event: Event
    sources: Tuple[ResolvedSource, ...]

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class MaterializedRule(object):
    signature: Signature
    events: Tuple[MaterializedEvent, ...]

    def __post_init__(self):
        object.__setattr__(self, "events", tuple(self.events))

    @property
    def metadata(self):
        return self.signature.metadata


@dataclass(frozen=True)
class ValidationIssue(object):
    scope: str
    code: str
    message: str
    path: str = "$"
    rule_name: Optional[str] = None
    rule_id: Optional[int] = None
    line: Optional[int] = None

    def __str__(self):
        return "{}: {} ({})".format(self.path, self.message, self.code)


@dataclass(frozen=True)
class BrokenRule(object):
    rule_name: str
    rule_id: Optional[int]
    issues: Tuple[ValidationIssue, ...]
    rule_version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True)
class ValidationResult(object):
    schema_version: Optional[str]
    ruleset: Optional[RuleSet]
    materialized_rules: Tuple[MaterializedRule, ...] = ()
    file_errors: Tuple[ValidationIssue, ...] = ()
    broken_rules: Tuple[BrokenRule, ...] = ()
    source_lines: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self):
        object.__setattr__(self, "materialized_rules", tuple(self.materialized_rules))
        object.__setattr__(self, "file_errors", tuple(self.file_errors))
        object.__setattr__(self, "broken_rules", tuple(self.broken_rules))
        object.__setattr__(self, "source_lines", frozen_mapping(self.source_lines))

    @property
    def usable_rules(self):
        return self.materialized_rules

    @property
    def file_valid(self):
        return not self.file_errors

    @property
    def activation_valid(self):
        return self.file_valid and bool(self.materialized_rules)
