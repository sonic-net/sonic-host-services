"""Authoritative Pydantic wire contract for DLDD schema version 0.0.1."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Dict, List, Literal, Optional, Union

import regex as bounded_regex
from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    StrictInt,
    StrictStr,
    Tag,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from typing_extensions import Annotated

from ..evaluators import EvaluationContractError, parse_integer
from ..logic import LogicSyntaxError, parse_logic
from ..models import (
    Actions as DomainActions,
    Conditions as DomainConditions,
    Evaluation as DomainEvaluation,
    Event as DomainEvent,
    LocalActions as DomainLocalActions,
    LogCollection as DomainLogCollection,
    Metadata as DomainMetadata,
    Operation as DomainOperation,
    RemoteActions as DomainRemoteActions,
    RepairActions as DomainRepairActions,
    Signature as DomainSignature,
    ValueConfig as DomainValueConfig,
    frozen_mapping,
)
from .base import (
    ContractModel,
    FiniteNumber,
    INT64_MIN,
    JsonValue,
    JsonInteger,
    NonEmptyString,
    NonNullJsonValue,
    NonNegativeInteger,
    NonNegativeSeconds,
    PositiveInteger,
    PositiveSeconds,
    SamplingInterval,
    UINT64_MAX,
    omitted_non_null_field,
)
from .errors import DomainConversionError


SCHEMA_VERSION = "0.0.1"
MAX_SIGNATURES = 1024
MAX_EVENTS_PER_SIGNATURE = 1000
MAX_REGEX_CHARACTERS = 4096
MAX_REGEX_NESTING = 64

BUILTIN_OPERATION_TYPES = frozenset(("cli", "dse", "i2c"))
VENDOR_RESERVED_OPERATION_FIELDS = frozenset(
    ("argv", "command", "max_output_bytes", "path")
)

RuleName = Annotated[
    StrictStr, Field(min_length=1, pattern=r"^[A-Za-z0-9_]+$")
]
SemanticVersion = Annotated[
    StrictStr, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
]
RuleId = Annotated[StrictInt, Field(ge=1_000_000, le=9_999_999)]
EventId = Annotated[StrictInt, Field(ge=1, le=999)]
MatchCount = Annotated[StrictInt, Field(ge=1, le=1000)]
MatchPeriod = Annotated[StrictInt, Field(ge=0, le=3600)]
LogicLookback = Annotated[StrictInt, Field(ge=0, le=86400)]
HexString = Annotated[StrictStr, Field(pattern=r"^0x[0-9A-Fa-f]+$")]
IntegerString = Annotated[
    StrictStr,
    Field(
        max_length=256,
        pattern=(
            r"^\s*[+-]?(?:0[bB][01]+|0[oO][0-7]+|"
            r"0[xX][0-9A-Fa-f]+|0+|[1-9][0-9]*)\s*$"
        ),
        json_schema_extra={
            "x-dldd-runtime-constraint": {
                "authority": "pydantic-runtime-contract",
                "kind": "parsed-integer-range",
                "minimum": INT64_MIN,
                "maximum": UINT64_MAX,
                "description": (
                    "After parsing decimal, binary, octal, or hexadecimal "
                    "notation, the value must be within the published range."
                ),
            }
        },
    ),
]
DSEReferenceString = Annotated[
    StrictStr,
    Field(
        pattern=(
            r"^(?:[A-Za-z0-9_.-]+:[A-Za-z_][A-Za-z0-9_]*\(\)|"
            r"\{[A-Za-z0-9_.?*\-]+\}:"
            r"\{[A-Za-z_][A-Za-z0-9_]*\(\)\})$"
        )
    ),
]
InstanceBinding = Annotated[StrictStr, Field(pattern=r"^[^:]+:.*$")]
NonEmptyStringList = Annotated[List[NonEmptyString], Field(min_length=1)]
NonEmptyInstanceList = Annotated[List[InstanceBinding], Field(min_length=1)]
ScalingValue = Union[FiniteNumber, Literal["N/A"]]

# Component types are vendor/platform identities. DLDD requires a usable
# string but deliberately does not maintain an allowlist: a platform can
# define any number of component classes without a schema revision.
ComponentType = NonEmptyString
SymptomType = Literal[
    "SYMPTOM_OVER_THRESHOLD",
    "SYMPTOM_UNDER_THRESHOLD",
    "SYMPTOM_MEMORY_ERRORS",
    "SYMPTOM_MISSING_COMPONENT",
    "SYMPTOM_COMM_ERROR",
    "SYMPTOM_UNKNOWN",
]
SeverityType = Literal["CRITICAL", "MAJOR", "WARNING", "MINOR", "UNKNOWN"]
ValueType = Literal[
    "binary",
    "hex",
    "int",
    "float",
    "string",
    "boolean",
    "json",
    "bytes",
    "N/A",
]
# OpenConfig remediation identities are extensible. Preserve any non-empty
# identity string and leave namespace/identity resolution to the controller's
# OpenConfig implementation rather than embedding a DLDD-side enum.
RemoteActionType = NonEmptyString


def _regex_nesting(pattern: str) -> int:
    nesting = 0
    maximum = 0
    escaped = False
    character_class = False
    for character in pattern:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "[" and not character_class:
            character_class = True
        elif character == "]" and character_class:
            character_class = False
        elif not character_class and character == "(":
            nesting += 1
            maximum = max(maximum, nesting)
        elif not character_class and character == ")":
            nesting = max(0, nesting - 1)
    return maximum


class ValueConfigV001(ContractModel):
    type: ValueType
    unit: NonEmptyString
    scaling: ScalingValue = "N/A"
    encoding: NonEmptyString = "N/A"


class MetadataV001(ContractModel):
    name: RuleName
    id: RuleId
    version: SemanticVersion
    description: NonEmptyString
    product_ids: NonEmptyStringList
    sw_versions: NonEmptyStringList
    component: ComponentType
    symptom: SymptomType
    error_type: NonEmptyString
    severity: SeverityType
    priority: NonNegativeInteger = 5
    tags: List[NonEmptyString] = Field(default_factory=list)


class I2CReadPathV001(ContractModel):
    bus: Union[NonEmptyString, NonEmptyStringList]
    chip_addr: HexString
    i2c_type: Literal["get"]
    command: HexString
    size: Literal["b", "w", "l"]
    scaling: ScalingValue = omitted_non_null_field()


class RedisPathV001(ContractModel):
    database: NonEmptyString
    table: NonEmptyString
    key: NonEmptyString
    path: NonEmptyString


class CLIPathV001(ContractModel):
    argv: NonEmptyStringList
    timeout: PositiveSeconds = omitted_non_null_field()


class FilePathV001(ContractModel):
    file: NonEmptyString
    format: NonEmptyString
    scaling: ScalingValue = omitted_non_null_field()
    unit: NonEmptyString = omitted_non_null_field()


class PlatformAPIHookPathV001(ContractModel):
    """Explicit vendor-owned platform API path envelope."""

    model_config = ConfigDict(extra="allow")

    __pydantic_extra__: Dict[str, JsonValue] = Field(init=False)
    hook: NonEmptyString


PlatformAPIPathV001 = Union[DSEReferenceString, PlatformAPIHookPathV001]


class EvaluationBaseV001(ContractModel):
    value_configs: ValueConfigV001 = omitted_non_null_field()


class MaskEvaluationV001(EvaluationBaseV001):
    type: Literal["mask"]
    logic: Literal["&"]
    value: Union[JsonInteger, IntegerString]

    @field_validator("value")
    @classmethod
    def validate_mask_value(cls, value):
        try:
            parsed = parse_integer(value)
            if not INT64_MIN <= parsed <= UINT64_MAX:
                raise ValueError("mask integer is outside supported 64-bit range")
        except (EvaluationContractError, UnicodeError, ValueError):
            raise PydanticCustomError(
                "invalid_mask_value",
                (
                    "mask value must use integer, decimal, binary, octal, "
                    "or hexadecimal notation"
                ),
            )
        return value


class ComparisonEvaluationV001(EvaluationBaseV001):
    type: Literal["comparison"]
    operator: Literal[">", "<", ">=", "<=", "==", "!="]
    value: Union[FiniteNumber, StrictStr]
    unit: NonEmptyString = omitted_non_null_field()


class StringEvaluationV001(EvaluationBaseV001):
    type: Literal["string"]
    operator: Literal["contains", "equals", "regex"]
    value: StrictStr
    case_sensitive: bool = True

    @model_validator(mode="after")
    def validate_regex(self):
        if self.operator != "regex":
            return self
        if len(self.value) > MAX_REGEX_CHARACTERS:
            raise PydanticCustomError(
                "invalid_regex",
                "regex exceeds {maximum} characters",
                {"maximum": MAX_REGEX_CHARACTERS},
            )
        if _regex_nesting(self.value) > MAX_REGEX_NESTING:
            raise PydanticCustomError(
                "invalid_regex",
                "regex nesting exceeds {maximum} levels",
                {"maximum": MAX_REGEX_NESTING},
            )
        try:
            bounded_regex.compile(self.value)
        except (bounded_regex.error, RecursionError):
            raise PydanticCustomError(
                "invalid_regex", "regular expression is not valid"
            )
        return self


class BooleanEvaluationV001(EvaluationBaseV001):
    type: Literal["boolean"]
    value: bool


class DSEEvaluationV001(EvaluationBaseV001):
    type: Literal["dse"]
    value: DSEReferenceString
    operator: Literal[
        ">", "<", ">=", "<=", "==", "!=", "equals", "not_equals"
    ] = omitted_non_null_field()


EvaluationV001 = Annotated[
    Union[
        MaskEvaluationV001,
        ComparisonEvaluationV001,
        StringEvaluationV001,
        BooleanEvaluationV001,
        DSEEvaluationV001,
    ],
    Field(discriminator="type"),
]


class EventBaseV001(ContractModel):
    id: EventId
    instances: NonEmptyInstanceList = Field(
        default_factory=list, validate_default=False
    )
    evaluation: EvaluationV001
    match_count: MatchCount
    match_period: MatchPeriod
    sampling_interval: SamplingInterval = omitted_non_null_field()
    async_collection: bool = Field(default=False, alias="async")

    @model_validator(mode="after")
    def validate_common_event_contract(self):
        if self.match_period == 0 and self.match_count != 1:
            raise PydanticCustomError(
                "invalid_match_window",
                (
                    "match_period 0 uses current-state semantics and requires "
                    "match_count 1"
                ),
            )
        instance_names = [item.split(":", 1)[0] for item in self.instances]
        if len(instance_names) != len(set(instance_names)):
            raise PydanticCustomError(
                "duplicate_instance", "component instances must be unique"
            )
        return self


class I2CEventV001(EventBaseV001):
    type: Literal["i2c"]
    path: I2CReadPathV001

    @model_validator(mode="after")
    def validate_positional_bus(self):
        if isinstance(self.path.bus, list):
            if not self.instances:
                raise PydanticCustomError(
                    "instance_path_mismatch",
                    "list-valued I2C bus requires positional instances",
                )
            if len(self.path.bus) != len(self.instances):
                raise PydanticCustomError(
                    "instance_path_mismatch",
                    "list-valued path field must match instances length",
                )
        return self


class RedisEventV001(EventBaseV001):
    type: Literal["redis"]
    path: RedisPathV001


class DSEEventV001(EventBaseV001):
    type: Literal["dse"]
    path: DSEReferenceString


class CLIEventV001(EventBaseV001):
    type: Literal["cli"]
    path: CLIPathV001


class FileEventV001(EventBaseV001):
    type: Literal["file"]
    path: FilePathV001


class SysfsEventV001(EventBaseV001):
    type: Literal["sysfs"]
    path: FilePathV001


class PlatformAPIEventV001(EventBaseV001):
    type: Literal["platform_api"]
    path: PlatformAPIPathV001

    @model_validator(mode="after")
    def validate_positional_vendor_path(self):
        if not self.instances or isinstance(self.path, str):
            return self
        path_values = {
            **self.path.model_dump(exclude_unset=True),
            **_model_extras(self.path),
        }
        for key, value in path_values.items():
            if (
                isinstance(value, list)
                and key != "argv"
                and len(value) != len(self.instances)
            ):
                raise PydanticCustomError(
                    "instance_path_mismatch",
                    "list-valued path field must match instances length",
                )
        return self


EventV001 = Annotated[
    Union[
        I2CEventV001,
        RedisEventV001,
        DSEEventV001,
        CLIEventV001,
        FileEventV001,
        SysfsEventV001,
        PlatformAPIEventV001,
    ],
    Field(discriminator="type"),
]


class EventWrapperV001(ContractModel):
    event: EventV001


class ConditionsV001(ContractModel):
    logic: NonEmptyString
    logic_lookback_time: LogicLookback
    events: Annotated[
        List[EventWrapperV001],
        Field(min_length=1, max_length=MAX_EVENTS_PER_SIGNATURE),
    ]

    @model_validator(mode="after")
    def validate_event_identity_and_logic(self):
        event_ids = [wrapper.event.id for wrapper in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise PydanticCustomError(
                "duplicate_event_id",
                "event IDs must be unique within a signature",
            )
        try:
            parse_logic(self.logic, event_ids)
        except LogicSyntaxError as error:
            raise PydanticCustomError(
                "invalid_logic", "{reason}", {"reason": str(error)}
            )
        return self


class CLIOperationV001(ContractModel):
    type: Literal["cli"]
    argv: NonEmptyStringList
    timeout: PositiveSeconds = omitted_non_null_field()
    max_output_bytes: PositiveInteger = omitted_non_null_field()


class DSEOperationV001(ContractModel):
    type: Literal["dse"]
    command: NonEmptyString
    timeout: PositiveSeconds = omitted_non_null_field()


class I2CActionPathV001(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"i2c_type": {"const": "set"}},
                        "required": ["i2c_type"],
                    },
                    "then": {"required": ["value"]},
                }
            ]
        }
    )

    bus: Union[NonEmptyString, NonEmptyStringList]
    chip_addr: HexString
    i2c_type: Literal["get", "set"]
    command: HexString
    size: Literal["b", "w", "l"]
    value: NonNullJsonValue = omitted_non_null_field()

    @model_validator(mode="after")
    def require_write_value(self):
        if self.i2c_type == "set" and "value" not in self.model_fields_set:
            raise PydanticCustomError(
                "missing_i2c_value", "set actions require a value"
            )
        return self


class I2COperationV001(ContractModel):
    type: Literal["i2c"]
    path: I2CActionPathV001
    timeout: PositiveSeconds = omitted_non_null_field()


def _vendor_operation_json_schema(schema):
    type_schema = schema.get("properties", {}).get("type")
    if type_schema is not None:
        type_schema["not"] = {"enum": sorted(BUILTIN_OPERATION_TYPES)}
    schema.setdefault("allOf", []).append(
        {
            "not": {
                "anyOf": [
                    {"required": [field]}
                    for field in sorted(VENDOR_RESERVED_OPERATION_FIELDS)
                ]
            }
        }
    )


class VendorOperationV001(ContractModel):
    """Bounded envelope for a platform-advertised action or query type."""

    model_config = ConfigDict(
        extra="allow", json_schema_extra=_vendor_operation_json_schema
    )

    __pydantic_extra__: Dict[str, JsonValue] = Field(init=False)
    type: NonEmptyString
    timeout: PositiveSeconds = omitted_non_null_field()

    @field_validator("type")
    @classmethod
    def reject_reserved_builtin_type(cls, value):
        if value in BUILTIN_OPERATION_TYPES:
            raise PydanticCustomError(
                "reserved_operation_type",
                "built-in operation type must use its built-in contract",
            )
        return value

    @model_validator(mode="after")
    def reject_reserved_builtin_fields(self):
        extras = frozenset(self.__pydantic_extra__ or {})
        reserved = sorted(extras & VENDOR_RESERVED_OPERATION_FIELDS)
        if reserved:
            raise PydanticCustomError(
                "reserved_operation_field",
                "vendor operation uses reserved field(s): {fields}",
                {"fields": ", ".join(reserved)},
            )
        return self


def _operation_discriminator(value):
    if isinstance(value, Mapping):
        operation_type = value.get("type")
    else:
        operation_type = getattr(value, "type", None)
    return (
        operation_type
        if isinstance(operation_type, str)
        and operation_type in BUILTIN_OPERATION_TYPES
        else "vendor"
    )


OperationV001 = Annotated[
    Union[
        Annotated[CLIOperationV001, Tag("cli")],
        Annotated[DSEOperationV001, Tag("dse")],
        Annotated[I2COperationV001, Tag("i2c")],
        Annotated[VendorOperationV001, Tag("vendor")],
    ],
    Discriminator(_operation_discriminator),
]


class LocalActionWrapperV001(ContractModel):
    action: OperationV001


class QueryWrapperV001(ContractModel):
    query: OperationV001


class LocalActionsV001(ContractModel):
    wait_period: NonNegativeSeconds
    action_list: Annotated[List[LocalActionWrapperV001], Field(min_length=1)]


class RemoteActionsV001(ContractModel):
    action_list: Annotated[List[RemoteActionType], Field(min_length=1)]
    time_window: PositiveSeconds


class RepairActionsV001(ContractModel):
    local_actions: LocalActionsV001 = omitted_non_null_field()
    remote_actions: RemoteActionsV001


class LogEntryV001(ContractModel):
    log: NonEmptyString


class LogCollectionV001(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [{"required": ["logs"]}, {"required": ["queries"]}]
        }
    )

    logs: Annotated[List[LogEntryV001], Field(min_length=1)] = Field(
        default_factory=list, validate_default=False
    )
    queries: Annotated[List[QueryWrapperV001], Field(min_length=1)] = Field(
        default_factory=list, validate_default=False
    )

    @model_validator(mode="after")
    def require_log_or_query(self):
        if not self.logs and not self.queries:
            raise PydanticCustomError(
                "empty_log_collection",
                "log collection requires at least one log or query",
            )
        return self


class ActionsV001(ContractModel):
    repair_actions: RepairActionsV001
    log_collection: LogCollectionV001 = omitted_non_null_field()


class SignatureV001(ContractModel):
    metadata: MetadataV001
    conditions: ConditionsV001
    actions: ActionsV001


class SignatureWrapperV001(ContractModel):
    signature: SignatureV001


class ShallowSignatureWrapperV001(ContractModel):
    # The shallow file gate owns only the wrapper shape.  The bounded parser
    # has already made the body safe to retain; the per-signature adapter owns
    # every nested type so one YAML-specific scalar cannot poison the file.
    signature: dict


class EnvelopeV001(ContractModel):
    schema_version: Literal[SCHEMA_VERSION]
    local_action_default_timeout: PositiveSeconds = omitted_non_null_field()
    signatures: Annotated[
        List[ShallowSignatureWrapperV001],
        Field(min_length=1, max_length=MAX_SIGNATURES),
    ]


class RulesDocumentV001(ContractModel):
    """Fully nested publication model; runtime uses :class:`EnvelopeV001`."""

    schema_version: Literal[SCHEMA_VERSION]
    local_action_default_timeout: PositiveSeconds = omitted_non_null_field()
    signatures: Annotated[
        List[SignatureWrapperV001], Field(min_length=1, max_length=MAX_SIGNATURES)
    ]


def _domain_value_config(config) -> DomainValueConfig:
    if config is None:
        return DomainValueConfig()
    return DomainValueConfig(
        type=config.type,
        unit=config.unit,
        scaling=config.scaling,
        encoding=config.encoding,
    )


def _model_extras(model) -> Dict[str, JsonValue]:
    return dict(getattr(model, "__pydantic_extra__", None) or {})


def _event_path_to_domain(path):
    if isinstance(path, str):
        return path
    if isinstance(path, I2CReadPathV001):
        result = {
            "bus": path.bus,
            "chip_addr": path.chip_addr,
            "i2c_type": path.i2c_type,
            "command": path.command,
            "size": path.size,
        }
        if "scaling" in path.model_fields_set:
            result["scaling"] = path.scaling
        return result
    if isinstance(path, RedisPathV001):
        return {
            "database": path.database,
            "table": path.table,
            "key": path.key,
            "path": path.path,
        }
    if isinstance(path, CLIPathV001):
        result = {"argv": list(path.argv)}
        if "timeout" in path.model_fields_set:
            result["timeout"] = path.timeout
        return result
    if isinstance(path, FilePathV001):
        result = {"file": path.file, "format": path.format}
        if "scaling" in path.model_fields_set:
            result["scaling"] = path.scaling
        if "unit" in path.model_fields_set:
            result["unit"] = path.unit
        return result
    if isinstance(path, PlatformAPIHookPathV001):
        result = {"hook": path.hook}
        result.update(_model_extras(path))
        return result
    raise TypeError(
        "unsupported validated event path model: {}".format(
            type(path).__name__
        )
    )


def _evaluation_to_domain(evaluation) -> DomainEvaluation:
    common = {
        "type": evaluation.type,
        "value": evaluation.value,
        "value_configs": _domain_value_config(evaluation.value_configs),
    }
    if isinstance(evaluation, MaskEvaluationV001):
        return DomainEvaluation(logic=evaluation.logic, **common)
    if isinstance(evaluation, ComparisonEvaluationV001):
        return DomainEvaluation(
            operator=evaluation.operator,
            unit=evaluation.unit,
            **common,
        )
    if isinstance(evaluation, StringEvaluationV001):
        return DomainEvaluation(
            operator=evaluation.operator,
            case_sensitive=evaluation.case_sensitive,
            **common,
        )
    if isinstance(evaluation, BooleanEvaluationV001):
        return DomainEvaluation(**common)
    if isinstance(evaluation, DSEEvaluationV001):
        return DomainEvaluation(operator=evaluation.operator, **common)
    raise TypeError(
        "unsupported validated evaluation model: {}".format(
            type(evaluation).__name__
        )
    )


def _event_to_domain(event) -> DomainEvent:
    return DomainEvent(
        id=event.id,
        type=event.type,
        path=_event_path_to_domain(event.path),
        evaluation=_evaluation_to_domain(event.evaluation),
        match_count=event.match_count,
        match_period=event.match_period,
        instances=tuple(event.instances),
        sampling_interval=event.sampling_interval,
        async_collection=event.async_collection,
    )


def _i2c_action_path_to_domain(path: I2CActionPathV001):
    result = {
        "bus": path.bus,
        "chip_addr": path.chip_addr,
        "i2c_type": path.i2c_type,
        "command": path.command,
        "size": path.size,
    }
    if "value" in path.model_fields_set:
        result["value"] = path.value
    return result


def _operation_to_domain(
    operation,
    *,
    default_timeout: Optional[int],
    query: bool,
    path: str,
) -> DomainOperation:
    timeout = operation.timeout
    if timeout is None and not query:
        if default_timeout is None:
            raise DomainConversionError(
                "missing_action_timeout",
                "local action requires timeout or local_action_default_timeout",
                path + ".timeout",
            )
        timeout = default_timeout

    if isinstance(operation, CLIOperationV001):
        return DomainOperation(
            type=operation.type,
            argv=tuple(operation.argv),
            timeout=timeout,
            max_output_bytes=operation.max_output_bytes,
        )
    if isinstance(operation, DSEOperationV001):
        return DomainOperation(
            type=operation.type,
            command=operation.command,
            timeout=timeout,
        )
    if isinstance(operation, I2COperationV001):
        return DomainOperation(
            type=operation.type,
            path=frozen_mapping(_i2c_action_path_to_domain(operation.path)),
            timeout=timeout,
        )
    if isinstance(operation, VendorOperationV001):
        return DomainOperation(
            type=operation.type,
            timeout=timeout,
            options=frozen_mapping(_model_extras(operation)),
        )
    raise TypeError(
        "unsupported validated operation model: {}".format(
            type(operation).__name__
        )
    )


def _actions_to_domain(
    actions: ActionsV001, default_timeout: Optional[int]
) -> DomainActions:
    repair = actions.repair_actions
    local = None
    if repair.local_actions is not None:
        operations = []
        for index, wrapper in enumerate(repair.local_actions.action_list):
            operations.append(
                _operation_to_domain(
                    wrapper.action,
                    default_timeout=default_timeout,
                    query=False,
                    path=(
                        "$.signature.actions.repair_actions.local_actions."
                        "action_list[{}].action"
                    ).format(index),
                )
            )
        local = DomainLocalActions(
            wait_period=repair.local_actions.wait_period,
            action_list=tuple(operations),
        )

    remote = DomainRemoteActions(
        action_list=tuple(repair.remote_actions.action_list),
        time_window=repair.remote_actions.time_window,
    )

    logs = None
    if actions.log_collection is not None:
        queries = []
        for index, wrapper in enumerate(actions.log_collection.queries):
            queries.append(
                _operation_to_domain(
                    wrapper.query,
                    default_timeout=default_timeout,
                    query=True,
                    path=(
                        "$.signature.actions.log_collection.queries[{}].query"
                    ).format(index),
                )
            )
        logs = DomainLogCollection(
            logs=tuple(item.log for item in actions.log_collection.logs),
            queries=tuple(queries),
        )

    return DomainActions(
        repair_actions=DomainRepairActions(
            remote_actions=remote,
            local_actions=local,
        ),
        log_collection=logs,
    )


def signature_v001_to_domain(
    wrapper: SignatureWrapperV001,
    *,
    local_action_default_timeout: Optional[int] = None,
) -> DomainSignature:
    """Convert one validated input DTO to the existing immutable rule model."""

    signature = wrapper.signature
    metadata = signature.metadata
    domain_metadata = DomainMetadata(
        name=metadata.name,
        id=metadata.id,
        version=metadata.version,
        description=metadata.description,
        product_ids=tuple(metadata.product_ids),
        sw_versions=tuple(metadata.sw_versions),
        component=metadata.component,
        symptom=metadata.symptom,
        error_type=metadata.error_type,
        severity=metadata.severity,
        priority=metadata.priority,
        tags=tuple(metadata.tags),
    )

    events = tuple(
        _event_to_domain(wrapper.event)
        for wrapper in signature.conditions.events
    )
    logic_tree = parse_logic(
        signature.conditions.logic, [event.id for event in events]
    )
    conditions = DomainConditions(
        logic=signature.conditions.logic,
        logic_tree=logic_tree,
        logic_lookback_time=signature.conditions.logic_lookback_time,
        events=events,
    )
    return DomainSignature(
        metadata=domain_metadata,
        conditions=conditions,
        actions=_actions_to_domain(
            signature.actions, local_action_default_timeout
        ),
    )


__all__ = (
    "ActionsV001",
    "BUILTIN_OPERATION_TYPES",
    "DomainConversionError",
    "EnvelopeV001",
    "EventV001",
    "EvaluationV001",
    "MetadataV001",
    "OperationV001",
    "RulesDocumentV001",
    "SCHEMA_VERSION",
    "ShallowSignatureWrapperV001",
    "SignatureV001",
    "SignatureWrapperV001",
    "VendorOperationV001",
    "signature_v001_to_domain",
)
