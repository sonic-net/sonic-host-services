"""Immutable rule models shared by DLDD validation and runtime code."""

from __future__ import absolute_import

from dataclasses import dataclass, field, fields, is_dataclass
import math
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

    def __post_init__(self):
        errors = value_config_contract_errors(self)
        if errors:
            raise ValueError(
                "invalid value config: {}".format("; ".join(errors))
            )

    @classmethod
    def from_mapping(cls, value):
        """Build validated value metadata from its wire representation."""

        if isinstance(value, cls):
            config = value
        else:
            if not isinstance(value, Mapping):
                raise TypeError("value config must be a mapping")
            unknown = set(value) - {"type", "unit", "scaling", "encoding"}
            if unknown:
                raise ValueError(
                    "unknown value config fields: {}".format(
                        ", ".join(sorted(str(item) for item in unknown))
                    )
                )
            config = cls(**dict(value))
        errors = value_config_contract_errors(config)
        if errors:
            raise ValueError(
                "invalid value config: {}".format("; ".join(errors))
            )
        return config

    def as_payload(self):
        return {
            "type": self.type,
            "unit": self.unit,
            "scaling": self.scaling,
            "encoding": self.encoding,
        }


def value_config_contract_errors(config):
    """Return canonical contract errors for typed rule or DSE value metadata."""

    if not isinstance(config, ValueConfig):
        return ("must be ValueConfig",)
    errors = []
    if not isinstance(config.type, str) or config.type not in VALUE_CONFIG_TYPES:
        errors.append("type must use a canonical value")
    if not isinstance(config.unit, str) or not config.unit:
        errors.append("unit must be a non-empty string")
    numeric_scaling = (
        isinstance(config.scaling, int)
        and not isinstance(config.scaling, bool)
    ) or (
        isinstance(config.scaling, float) and math.isfinite(config.scaling)
    )
    if not (numeric_scaling or config.scaling == "N/A"):
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
    sampling_interval: Optional[int] = None
    async_collection: bool = False

    def __post_init__(self):
        object.__setattr__(self, "path", freeze_value(self.path))
        object.__setattr__(self, "instances", tuple(self.instances))
        object.__setattr__(self, "async_collection", bool(self.async_collection))

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

    def as_runtime_payload(self) -> Mapping[str, Any]:
        """Return the canonical action/query dispatch representation."""

        reserved = {
            "type",
            "command",
            "argv",
            "path",
            "timeout",
            "max_output_bytes",
            "executor",
            "materialized_operation",
        }
        payload = {
            key: value
            for key, value in self.options.items()
            if key not in reserved
        }
        payload["type"] = self.type
        if self.command is not None:
            payload["command"] = self.command
        if self.argv:
            payload["argv"] = list(self.argv)
        if self.path:
            payload["path"] = dict(self.path)
        if self.timeout is not None:
            payload["timeout"] = self.timeout
        if self.max_output_bytes is not None:
            payload["max_output_bytes"] = self.max_output_bytes
        if self.executor is not None:
            payload["executor"] = self.executor
            payload["materialized_operation"] = self
        return payload


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
    schema_version: str
    metadata: Metadata
    conditions: Conditions
    actions: Actions

    def __post_init__(self):
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("signature schema_version must be a non-empty string")


@dataclass(frozen=True)
class RuleSet(object):
    schema_version: str
    signatures: Tuple[Signature, ...]
    local_action_default_timeout: Optional[int] = None

    def __post_init__(self):
        signatures = tuple(self.signatures)
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("ruleset schema_version must be a non-empty string")
        if any(
            signature.schema_version != self.schema_version
            for signature in signatures
        ):
            raise ValueError(
                "ruleset signatures must match the ruleset schema_version"
            )
        object.__setattr__(self, "signatures", signatures)


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
    dse_context: Any = None
    dse_source_handle: Any = None
    dse_evaluation_handle: Any = None

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
