"""Versioned static, semantic, and materialization validation for DLDD rules."""

from __future__ import absolute_import

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
import json
import math
import os
import re
from types import MappingProxyType
from typing import Mapping, Optional

import regex as bounded_regex

try:
    import yaml
except ImportError:  # pragma: no cover - SONiC images provide PyYAML
    yaml = None

from .dse import (
    DSEContext,
    DSEError,
    DSEReferenceError,
    DSERegistry,
    EMPTY_DSE_REGISTRY,
    parse_reference,
)
from .evaluators import EvaluationContractError, parse_integer
from .logic import LogicSyntaxError, parse_logic
from .models import (
    Actions,
    BrokenRule,
    Conditions,
    Evaluation,
    Event,
    LocalActions,
    LogCollection,
    MaterializedEvent,
    MaterializedRule,
    Metadata,
    Operation,
    RemoteActions,
    RepairActions,
    ResolvedSource,
    RuleSet,
    Signature,
    ValidationIssue,
    ValidationResult,
    VALUE_CONFIG_TYPES,
    ValueConfig,
    freeze_value,
    frozen_mapping,
)
from .schema_registry import DEFAULT_SCHEMA_REGISTRY, SchemaRegistryError


SUPPORTED_SCHEMA_VERSIONS = frozenset(DEFAULT_SCHEMA_REGISTRY.versions)
SCHEMA_VERSION = "0.0.1"
SCHEMA_PATH = DEFAULT_SCHEMA_REGISTRY.schema_path(SCHEMA_VERSION)

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DOCUMENT_DEPTH = 64
MAX_DOCUMENT_NODES = 100000
MAX_COLLECTION_ITEMS = 10000
MAX_SCALAR_BYTES = 1024 * 1024
MAX_SIGNATURES = 1024
MAX_EVENTS_PER_SIGNATURE = 1000
MAX_YAML_ALIASES = 0
MAX_REGEX_CHARACTERS = 4096
MAX_REGEX_NESTING = 64

EVENT_TYPES = frozenset(
    ("i2c", "redis", "dse", "cli", "file", "sysfs", "platform_api")
)
EVALUATION_TYPES = frozenset(("mask", "comparison", "string", "boolean", "dse"))
COMPONENT_TYPES = frozenset(
    ("PSU", "FAN", "CHASSIS", "SSD", "CPU", "MEMORY", "ASIC", "TRANSCEIVER")
)
SEVERITIES = frozenset(("CRITICAL", "MAJOR", "WARNING", "MINOR", "UNKNOWN"))
OPENCONFIG_SYMPTOMS = frozenset(
    (
        "SYMPTOM_OVER_THRESHOLD",
        "SYMPTOM_UNDER_THRESHOLD",
        "SYMPTOM_MEMORY_ERRORS",
        "SYMPTOM_MISSING_COMPONENT",
        "SYMPTOM_COMM_ERROR",
        "SYMPTOM_UNKNOWN",
    )
)
REMOTE_ACTIONS = frozenset(
    (
        "ACTION_RESEAT",
        "ACTION_WARM_REBOOT",
        "ACTION_COLD_REBOOT",
        "ACTION_POWER_CYCLE",
        "ACTION_FACTORY_RESET",
        "ACTION_REPLACE",
    )
)
VALUE_TYPES = VALUE_CONFIG_TYPES
COMPARISON_OPERATORS = frozenset((">", "<", ">=", "<=", "==", "!="))
DSE_OPERATORS = COMPARISON_OPERATORS | frozenset(("equals", "not_equals"))
STRING_OPERATORS = frozenset(("contains", "equals", "regex"))
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_RULE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_HEX = re.compile(r"^0x[0-9A-Fa-f]+$")


class CompatibilityMatcher(object, metaclass=ABCMeta):
    """Platform-owned matching contract for product and software versions."""

    @abstractmethod
    def product_matches(self, current_product, supported_products):
        pass

    @abstractmethod
    def software_matches(self, current_version, supported_versions):
        pass


class ExactCompatibilityMatcher(CompatibilityMatcher):
    def product_matches(self, current_product, supported_products):
        return current_product in supported_products

    def software_matches(self, current_version, supported_versions):
        return current_version in supported_versions


@dataclass(frozen=True)
class ValidationContext(object):
    product_id: Optional[str] = None
    software_version: Optional[str] = None
    require_compatibility_identity: bool = False
    dse_registry: DSERegistry = field(default_factory=lambda: EMPTY_DSE_REGISTRY)
    compatibility_matcher: CompatibilityMatcher = field(
        default_factory=ExactCompatibilityMatcher
    )


class RulesParseError(ValueError):
    """Parse failure carrying a one-based source line when available."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


if yaml is not None:
    class _UniqueKeySafeLoader(yaml.SafeLoader):
        """SafeLoader variant that rejects ambiguous duplicate keys."""

        def construct_mapping(self, node, deep=False):
            keys = set()
            for key_node, unused_value_node in node.value:
                key = self.construct_object(key_node, deep=False)
                try:
                    duplicate = key in keys
                except TypeError:
                    duplicate = False
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found duplicate key {!r}".format(key),
                        key_node.start_mark,
                    )
                try:
                    keys.add(key)
                except TypeError:
                    pass
            return super().construct_mapping(node, deep=deep)
else:  # pragma: no cover - SONiC images provide PyYAML
    _UniqueKeySafeLoader = None


def _bounded_text(value):
    if isinstance(value, str):
        size = len(value.encode("utf-8"))
        text = value
    elif isinstance(value, bytes):
        size = len(value)
        if size > MAX_SOURCE_BYTES:
            raise RulesParseError(
                "rules source exceeds {} bytes".format(MAX_SOURCE_BYTES), 1
            )
        text = value.decode("utf-8")
    else:
        raise TypeError("rules source must produce text or bytes")
    if size > MAX_SOURCE_BYTES:
        raise RulesParseError(
            "rules source exceeds {} bytes".format(MAX_SOURCE_BYTES), 1
        )
    return text


def _reject_duplicate_json_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RulesParseError(
                "duplicate JSON key {!r}".format(name)
            )
        result[name] = value
    return result


def _reject_nonfinite_json_number(value):
    raise RulesParseError(
        "non-finite JSON number {!r} is not allowed".format(value), 1
    )


def _enforce_document_limits(document):
    stack = [(document, 0, frozenset())]
    nodes = 0
    while stack:
        value, depth, ancestors = stack.pop()
        nodes += 1
        if nodes > MAX_DOCUMENT_NODES:
            raise RulesParseError(
                "rules document exceeds {} nodes".format(MAX_DOCUMENT_NODES),
                1,
            )
        if depth > MAX_DOCUMENT_DEPTH:
            raise RulesParseError(
                "rules document exceeds nesting depth {}".format(
                    MAX_DOCUMENT_DEPTH
                ),
                1,
            )
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_SCALAR_BYTES:
            raise RulesParseError(
                "rules document contains a scalar larger than {} bytes".format(
                    MAX_SCALAR_BYTES
                ),
                1,
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise RulesParseError("non-finite numbers are not allowed", 1)
        if not isinstance(value, (Mapping, list, tuple)):
            continue
        identity = id(value)
        if identity in ancestors:
            raise RulesParseError("recursive aliases are not allowed", 1)
        if len(value) > MAX_COLLECTION_ITEMS:
            raise RulesParseError(
                "rules document collection exceeds {} items".format(
                    MAX_COLLECTION_ITEMS
                ),
                1,
            )
        nested = ancestors | {identity}
        children = value.values() if isinstance(value, Mapping) else value
        stack.extend((item, depth + 1, nested) for item in children)


def _check_yaml_alias_limit(text):
    aliases = 0
    try:
        events = yaml.parse(text, Loader=_UniqueKeySafeLoader)
        for event in events:
            if isinstance(event, yaml.events.AliasEvent):
                aliases += 1
                if aliases > MAX_YAML_ALIASES:
                    mark = getattr(event, "start_mark", None)
                    raise RulesParseError(
                        "YAML aliases are not allowed",
                        mark.line + 1 if mark is not None else 1,
                    )
    except RulesParseError:
        raise
    except Exception:
        # The authoritative loader below will produce the parse diagnostic.
        return


def _build_source_lines(root_node):
    """Map validator JSONPath-like paths to one-based YAML/JSON node lines."""

    lines = {}

    def visit(node, path, ancestors):
        if node is None:
            return
        mark = getattr(node, "start_mark", None)
        if mark is not None:
            lines.setdefault(path, mark.line + 1)
        identity = id(node)
        if identity in ancestors:
            return
        nested = ancestors | {identity}
        if yaml is not None and isinstance(node, yaml.nodes.MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                child_path = "{}.{}".format(path, key)
                key_mark = getattr(key_node, "start_mark", None)
                if key_mark is not None:
                    lines[child_path] = key_mark.line + 1
                visit(value_node, child_path, nested)
        elif yaml is not None and isinstance(node, yaml.nodes.SequenceNode):
            for index, item_node in enumerate(node.value):
                visit(item_node, "{}[{}]".format(path, index), nested)

    visit(root_node, "$", set())
    return lines


def source_line_for_path(source_lines, path):
    """Return an exact source line or the nearest mapped parent line."""

    probe = path
    while probe:
        line = source_lines.get(probe)
        if line is not None:
            return line
        if probe.endswith("]") and "[" in probe:
            probe = probe[: probe.rfind("[")]
        elif "." in probe:
            probe = probe.rsplit(".", 1)[0]
        else:
            break
    return source_lines.get("$")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def _regex_nesting(pattern):
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


def _issue(issues, code, message, path):
    issues.append((code, message, path))


def _require_mapping(value, issues, path):
    if not isinstance(value, Mapping):
        _issue(issues, "invalid_type", "must be an object", path)
        return None
    return value


def _require_string(mapping, key, issues, path, nonempty=True):
    value = mapping.get(key)
    field_path = "{}.{}".format(path, key)
    if not isinstance(value, str):
        _issue(issues, "invalid_type", "must be a string", field_path)
        return None
    if nonempty and not value:
        _issue(issues, "invalid_value", "must not be empty", field_path)
        return None
    return value


def _require_integer(mapping, key, issues, path, minimum=None, maximum=None):
    value = mapping.get(key)
    field_path = "{}.{}".format(path, key)
    if not _is_int(value):
        _issue(issues, "invalid_type", "must be an integer", field_path)
        return None
    if minimum is not None and value < minimum:
        _issue(
            issues,
            "out_of_range",
            "must be at least {}".format(minimum),
            field_path,
        )
    if maximum is not None and value > maximum:
        _issue(
            issues,
            "out_of_range",
            "must be at most {}".format(maximum),
            field_path,
        )
    return value


def _string_list(mapping, key, issues, path, required=True, nonempty=True):
    value = mapping.get(key)
    field_path = "{}.{}".format(path, key)
    if value is None and not required:
        return ()
    if not isinstance(value, list):
        _issue(issues, "invalid_type", "must be a list", field_path)
        return ()
    if nonempty and not value:
        _issue(issues, "invalid_value", "must not be empty", field_path)
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _issue(
                issues,
                "invalid_type",
                "must be a non-empty string",
                "{}[{}]".format(field_path, index),
            )
        else:
            result.append(item)
    return tuple(result)


def _validate_value_config(value, issues, path):
    if value is None:
        return ValueConfig()
    config = _require_mapping(value, issues, path)
    if config is None:
        return ValueConfig()
    value_type = _require_string(config, "type", issues, path)
    unit = _require_string(config, "unit", issues, path)
    if value_type is not None and value_type not in VALUE_TYPES:
        _issue(issues, "unsupported_value_type", "unsupported value type", path + ".type")
    scaling = config.get("scaling", "N/A")
    if not (_is_number(scaling) or scaling == "N/A"):
        _issue(
            issues,
            "invalid_type",
            "must be a number or 'N/A'",
            path + ".scaling",
        )
        scaling = "N/A"
    encoding = config.get("encoding", "N/A")
    if not isinstance(encoding, str) or not encoding:
        _issue(issues, "invalid_type", "must be a non-empty string", path + ".encoding")
        encoding = "N/A"
    return ValueConfig(
        type=value_type or "N/A",
        unit=unit or "N/A",
        scaling=scaling,
        encoding=encoding,
    )


def _validate_dse_reference(value, issues, path):
    if not isinstance(value, str) or not value:
        return
    try:
        parse_reference(value)
    except DSEReferenceError as error:
        _issue(issues, "invalid_dse_reference", str(error), path)


def _validate_evaluation(value, issues, path):
    evaluation = _require_mapping(value, issues, path)
    if evaluation is None:
        return None
    kind = _require_string(evaluation, "type", issues, path)
    if kind not in EVALUATION_TYPES:
        if kind is not None:
            _issue(issues, "unsupported_evaluation", "unsupported evaluation type", path + ".type")
        return None

    configured = evaluation.get("value")
    if "value" not in evaluation:
        _issue(issues, "missing_field", "is required", path + ".value")
    operator = evaluation.get("operator")
    logic = evaluation.get("logic")
    unit = evaluation.get("unit")
    case_sensitive = evaluation.get("case_sensitive", True)

    if kind == "mask":
        if logic != "&":
            _issue(issues, "invalid_mask_logic", "schema 0.0.1 only supports '&'", path + ".logic")
        if not (_is_int(configured) or isinstance(configured, str)):
            _issue(issues, "invalid_type", "mask value must be an integer or string", path + ".value")
        else:
            try:
                parse_integer(configured)
            except (EvaluationContractError, UnicodeError, ValueError):
                _issue(
                    issues,
                    "invalid_mask_value",
                    "mask value must use integer, decimal, binary, octal, or hexadecimal notation",
                    path + ".value",
                )
    elif kind == "comparison":
        if operator not in COMPARISON_OPERATORS:
            _issue(issues, "invalid_operator", "unsupported comparison operator", path + ".operator")
        if not (_is_number(configured) or isinstance(configured, str)):
            _issue(issues, "invalid_type", "comparison value must be numeric or string", path + ".value")
        if unit is not None and (not isinstance(unit, str) or not unit):
            _issue(issues, "invalid_type", "unit must be a non-empty string", path + ".unit")
    elif kind == "string":
        if operator not in STRING_OPERATORS:
            _issue(issues, "invalid_operator", "unsupported string operator", path + ".operator")
        if not isinstance(configured, str):
            _issue(issues, "invalid_type", "string evaluation value must be a string", path + ".value")
        if not isinstance(case_sensitive, bool):
            _issue(issues, "invalid_type", "must be a boolean", path + ".case_sensitive")
            case_sensitive = True
        if operator == "regex" and isinstance(configured, str):
            if len(configured) > MAX_REGEX_CHARACTERS:
                _issue(
                    issues,
                    "invalid_regex",
                    "regex exceeds {} characters".format(
                        MAX_REGEX_CHARACTERS
                    ),
                    path + ".value",
                )
            elif _regex_nesting(configured) > MAX_REGEX_NESTING:
                _issue(
                    issues,
                    "invalid_regex",
                    "regex nesting exceeds {} levels".format(
                        MAX_REGEX_NESTING
                    ),
                    path + ".value",
                )
            else:
                try:
                    bounded_regex.compile(configured)
                except (bounded_regex.error, RecursionError) as error:
                    _issue(
                        issues,
                        "invalid_regex",
                        str(error),
                        path + ".value",
                    )
    elif kind == "boolean":
        if not isinstance(configured, bool):
            _issue(issues, "invalid_type", "boolean evaluation value must be a boolean", path + ".value")
    elif kind == "dse":
        if not isinstance(configured, str) or not configured:
            _issue(issues, "invalid_type", "DSE evaluation value must be a reference string", path + ".value")
        else:
            _validate_dse_reference(configured, issues, path + ".value")
        if operator is not None and operator not in DSE_OPERATORS:
            _issue(issues, "invalid_operator", "unsupported DSE comparison operator", path + ".operator")

    configs = _validate_value_config(
        evaluation.get("value_configs"), issues, path + ".value_configs"
    )
    return Evaluation(
        type=kind,
        value=freeze_value(configured),
        operator=operator,
        logic=logic,
        unit=unit,
        case_sensitive=case_sensitive,
        value_configs=configs,
    )


def _validate_i2c_path(value, issues, path, monitoring=True):
    target = _require_mapping(value, issues, path)
    if target is None:
        return
    bus = target.get("bus")
    if not (
        (isinstance(bus, str) and bus)
        or (
            isinstance(bus, (list, tuple))
            and bool(bus)
            and all(isinstance(item, str) and item for item in bus)
        )
    ):
        _issue(issues, "invalid_i2c_bus", "must be a string or non-empty string list", path + ".bus")
    for key in ("chip_addr", "command"):
        item = _require_string(target, key, issues, path)
        if item is not None and _HEX.match(item) is None:
            _issue(issues, "invalid_hex", "must use 0x hexadecimal notation", path + "." + key)
    operation = _require_string(target, "i2c_type", issues, path)
    permitted = ("get",) if monitoring else ("get", "set")
    if operation is not None and operation not in permitted:
        _issue(issues, "invalid_i2c_operation", "must be one of {}".format(permitted), path + ".i2c_type")
    size = _require_string(target, "size", issues, path)
    if size is not None and size not in ("b", "w", "l"):
        _issue(issues, "invalid_i2c_size", "must be 'b', 'w', or 'l'", path + ".size")
    if not monitoring and operation == "set" and "value" not in target:
        _issue(issues, "missing_field", "set actions require a value", path + ".value")


def _validate_argv(value, issues, path):
    # Parsed rule documents contain lists.  Frozen Event/ResolvedSource models
    # deliberately convert them to tuples before materialization revalidates a
    # resolved source, so both sequence representations are valid here.
    if not isinstance(value, (list, tuple)) or not value:
        _issue(issues, "invalid_argv", "must be a non-empty argv list", path)
        return ()
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            _issue(issues, "invalid_argv", "argv entries must be non-empty strings", "{}[{}]".format(path, index))
        else:
            result.append(item)
    return tuple(result)


def _validate_source_path(kind, value, instances, issues, path, registry):
    if kind == "i2c":
        _validate_i2c_path(value, issues, path, monitoring=True)
        if (
            isinstance(value, Mapping)
            and isinstance(value.get("bus"), (list, tuple))
            and not instances
        ):
            _issue(
                issues,
                "instance_path_mismatch",
                "list-valued I2C bus requires positional instances",
                path + ".bus",
            )
    elif kind == "redis":
        source = _require_mapping(value, issues, path)
        if source is not None:
            for key in ("database", "table", "key", "path"):
                _require_string(source, key, issues, path)
    elif kind == "dse":
        if not isinstance(value, str) or not value:
            _issue(issues, "invalid_type", "DSE path must be a reference string", path)
        else:
            _validate_dse_reference(value, issues, path)
    elif kind == "cli":
        source = _require_mapping(value, issues, path)
        if source is not None:
            _validate_argv(source.get("argv"), issues, path + ".argv")
            if "timeout" in source:
                _require_integer(source, "timeout", issues, path, minimum=1)
    elif kind in ("file", "sysfs"):
        source = _require_mapping(value, issues, path)
        if source is not None:
            _require_string(source, "file", issues, path)
            _require_string(source, "format", issues, path)
            if "scaling" in source and not (
                _is_number(source.get("scaling")) or source.get("scaling") == "N/A"
            ):
                _issue(issues, "invalid_type", "must be numeric or 'N/A'", path + ".scaling")
            if "unit" in source and not isinstance(source.get("unit"), str):
                _issue(issues, "invalid_type", "must be a string", path + ".unit")
    elif kind == "platform_api":
        if isinstance(value, Mapping):
            _require_string(value, "hook", issues, path)
        elif isinstance(value, str) and value:
            _validate_dse_reference(value, issues, path)
        else:
            _issue(
                issues,
                "invalid_platform_api_path",
                "must be a DSE reference string or an object with a hook",
                path,
            )
    elif kind is not None:
        event_path = path.rsplit(".", 1)[0]
        _issue(
            issues,
            "unsupported_event_type",
            "unsupported event type",
            event_path + ".type",
        )

    if instances and isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, list) and key != "argv" and len(item) != len(instances):
                _issue(
                    issues,
                    "instance_path_mismatch",
                    "list-valued path field must match instances length",
                    "{}.{}".format(path, key),
                )


def _validate_event(value, issues, path, registry):
    wrapper = _require_mapping(value, issues, path)
    if wrapper is None:
        return None
    event = _require_mapping(wrapper.get("event"), issues, path + ".event")
    if event is None:
        return None
    event_path = path + ".event"
    event_id = _require_integer(event, "id", issues, event_path, minimum=1, maximum=999)
    kind = _require_string(event, "type", issues, event_path)
    instances = _string_list(event, "instances", issues, event_path, required=False, nonempty=True)
    instance_names = []
    for index, item in enumerate(instances):
        if ":" not in item or not item.split(":", 1)[0]:
            _issue(
                issues,
                "invalid_instance",
                "must use DeviceName:PathIdentifier form",
                "{}.instances[{}]".format(event_path, index),
            )
        else:
            instance_names.append(item.split(":", 1)[0])
    if len(set(instance_names)) != len(instance_names):
        _issue(issues, "duplicate_instance", "component instances must be unique", event_path + ".instances")

    if "path" not in event:
        _issue(issues, "missing_field", "is required", event_path + ".path")
    _validate_source_path(
        kind,
        event.get("path"),
        instances,
        issues,
        event_path + ".path",
        registry,
    )
    evaluation = _validate_evaluation(
        event.get("evaluation"), issues, event_path + ".evaluation"
    )
    match_count = _require_integer(event, "match_count", issues, event_path, minimum=1, maximum=1000)
    match_period = _require_integer(event, "match_period", issues, event_path, minimum=0, maximum=3600)
    if match_period == 0 and match_count not in (None, 1):
        _issue(
            issues,
            "invalid_match_window",
            "match_period 0 uses current-state semantics and requires match_count 1",
            event_path + ".match_count",
        )
    if event_id is None or kind is None or evaluation is None or match_count is None or match_period is None:
        return None
    return Event(
        id=event_id,
        type=kind,
        path=freeze_value(event.get("path")),
        evaluation=evaluation,
        match_count=match_count,
        match_period=match_period,
        instances=instances,
    )


def _validate_operation(value, issues, path, default_timeout, registry, query=False):
    wrapper_name = "query" if query else "action"
    wrapper = _require_mapping(value, issues, path)
    if wrapper is None:
        return None
    operation = _require_mapping(wrapper.get(wrapper_name), issues, path + "." + wrapper_name)
    if operation is None:
        return None
    op_path = path + "." + wrapper_name
    kind = _require_string(operation, "type", issues, op_path)
    command = None
    argv = ()
    target = {}

    if kind == "dse":
        command = _require_string(operation, "command", issues, op_path)
    elif kind == "cli":
        argv = _validate_argv(operation.get("argv"), issues, op_path + ".argv")
    elif kind == "i2c":
        if (
            query
            and kind not in registry.query_types
            and not registry.allow_unadvertised_operations
        ):
            _issue(
                issues,
                "unsupported_operation_type",
                "i2c queries require explicit platform support",
                op_path + ".type",
            )
        else:
            _validate_i2c_path(
                operation.get("path"),
                issues,
                op_path + ".path",
                monitoring=False,
            )
            target = (
                operation.get("path")
                if isinstance(operation.get("path"), Mapping)
                else {}
            )
    elif kind in (registry.query_types if query else registry.action_types):
        pass
    elif kind is not None:
        # Vendor operation types are structurally valid at the static-schema
        # layer.  Activation materialization below requires the installed
        # platform DSE hook to advertise and validate them.
        if not registry.allow_unadvertised_operations:
            _issue(
                issues,
                "unsupported_operation_type",
                "unsupported {} type".format(wrapper_name),
                op_path + ".type",
            )

    timeout = operation.get("timeout")
    if timeout is not None:
        timeout = _require_integer(operation, "timeout", issues, op_path, minimum=1)
    elif not query:
        if default_timeout is None:
            _issue(
                issues,
                "missing_action_timeout",
                "local action requires timeout or local_action_default_timeout",
                op_path + ".timeout",
            )
        else:
            timeout = default_timeout
    max_output = operation.get("max_output_bytes")
    if max_output is not None:
        if kind != "cli":
            _issue(issues, "invalid_field", "max_output_bytes is only valid for CLI", op_path + ".max_output_bytes")
        max_output = _require_integer(operation, "max_output_bytes", issues, op_path, minimum=1)
    known = {"type", "command", "argv", "path", "timeout", "max_output_bytes"}
    options = {key: item for key, item in operation.items() if key not in known}
    if kind is None:
        return None
    return Operation(
        type=kind,
        command=command,
        argv=argv,
        path=frozen_mapping(target),
        timeout=timeout,
        max_output_bytes=max_output,
        options=frozen_mapping(options),
    )


def _validate_actions(value, issues, path, default_timeout, registry):
    actions = _require_mapping(value, issues, path)
    if actions is None:
        return None
    repairs = _require_mapping(actions.get("repair_actions"), issues, path + ".repair_actions")
    if repairs is None:
        return None
    repair_path = path + ".repair_actions"
    remote = _require_mapping(repairs.get("remote_actions"), issues, repair_path + ".remote_actions")
    remote_model = None
    if remote is not None:
        action_list = _string_list(remote, "action_list", issues, repair_path + ".remote_actions")
        for index, action in enumerate(action_list):
            if action not in REMOTE_ACTIONS:
                _issue(
                    issues,
                    "unsupported_remote_action",
                    "unsupported remediation identity",
                    "{}.remote_actions.action_list[{}]".format(repair_path, index),
                )
        time_window = _require_integer(remote, "time_window", issues, repair_path + ".remote_actions", minimum=1)
        if time_window is not None:
            remote_model = RemoteActions(action_list=action_list, time_window=time_window)

    local_model = None
    if "local_actions" in repairs:
        local = _require_mapping(repairs.get("local_actions"), issues, repair_path + ".local_actions")
        if local is not None:
            wait_period = _require_integer(local, "wait_period", issues, repair_path + ".local_actions", minimum=0)
            action_values = local.get("action_list")
            local_operations = []
            if not isinstance(action_values, list) or not action_values:
                _issue(
                    issues,
                    "invalid_action_list",
                    "must be a non-empty list",
                    repair_path + ".local_actions.action_list",
                )
            else:
                for index, item in enumerate(action_values):
                    operation = _validate_operation(
                        item,
                        issues,
                        "{}.local_actions.action_list[{}]".format(repair_path, index),
                        default_timeout,
                        registry,
                    )
                    if operation is not None:
                        local_operations.append(operation)
            if wait_period is not None:
                local_model = LocalActions(wait_period=wait_period, action_list=tuple(local_operations))

    log_model = None
    if "log_collection" in actions:
        log = _require_mapping(actions.get("log_collection"), issues, path + ".log_collection")
        if log is not None:
            logs = []
            queries = []
            log_values = log.get("logs", [])
            query_values = log.get("queries", [])
            if "logs" in log:
                if not isinstance(log_values, list) or not log_values:
                    _issue(issues, "invalid_logs", "must be a non-empty list", path + ".log_collection.logs")
                else:
                    for index, item in enumerate(log_values):
                        entry = _require_mapping(item, issues, "{}.log_collection.logs[{}]".format(path, index))
                        if entry is not None:
                            value = _require_string(
                                entry,
                                "log",
                                issues,
                                "{}.log_collection.logs[{}]".format(path, index),
                            )
                            if value is not None:
                                logs.append(value)
            if "queries" in log:
                if not isinstance(query_values, list) or not query_values:
                    _issue(issues, "invalid_queries", "must be a non-empty list", path + ".log_collection.queries")
                else:
                    for index, item in enumerate(query_values):
                        operation = _validate_operation(
                            item,
                            issues,
                            "{}.log_collection.queries[{}]".format(path, index),
                            default_timeout,
                            registry,
                            query=True,
                        )
                        if operation is not None:
                            queries.append(operation)
            if not logs and not queries:
                _issue(issues, "empty_log_collection", "requires at least one log or query", path + ".log_collection")
            log_model = LogCollection(logs=tuple(logs), queries=tuple(queries))

    if remote_model is None:
        return None
    return Actions(
        repair_actions=RepairActions(
            remote_actions=remote_model, local_actions=local_model
        ),
        log_collection=log_model,
    )


def _validate_signature(value, index, default_timeout, registry):
    base_path = "$.signatures[{}].signature".format(index)
    issues = []
    signature = value.get("signature") if isinstance(value, Mapping) else None
    if not isinstance(signature, Mapping):
        _issue(issues, "invalid_signature", "signature wrapper must contain an object", base_path)
        return None, issues

    metadata = _require_mapping(signature.get("metadata"), issues, base_path + ".metadata")
    metadata_model = None
    if metadata is not None:
        path = base_path + ".metadata"
        name = _require_string(metadata, "name", issues, path)
        if name is not None and _RULE_NAME.match(name) is None:
            _issue(issues, "invalid_rule_name", "must contain only letters, digits, and underscores", path + ".name")
        rule_id = _require_integer(metadata, "id", issues, path, minimum=1000000, maximum=9999999)
        version = _require_string(metadata, "version", issues, path)
        if version is not None and _SEMVER.match(version) is None:
            _issue(issues, "invalid_semver", "must use MAJOR.MINOR.PATCH", path + ".version")
        description = _require_string(metadata, "description", issues, path)
        products = _string_list(metadata, "product_ids", issues, path)
        software = _string_list(metadata, "sw_versions", issues, path)
        component = _require_string(metadata, "component", issues, path)
        if component is not None and component not in COMPONENT_TYPES:
            _issue(issues, "unsupported_component", "unsupported component", path + ".component")
        symptom = _require_string(metadata, "symptom", issues, path)
        if symptom is not None and symptom not in OPENCONFIG_SYMPTOMS:
            _issue(
                issues,
                "invalid_symptom",
                "must be a schema 0.0.1 OpenConfig Healthz symptom identity",
                path + ".symptom",
            )
        error_type = _require_string(metadata, "error_type", issues, path)
        severity = _require_string(metadata, "severity", issues, path)
        if severity is not None and severity not in SEVERITIES:
            _issue(issues, "unsupported_severity", "unsupported severity", path + ".severity")
        priority = metadata.get("priority", 5)
        if not _is_int(priority) or priority < 0:
            _issue(issues, "invalid_priority", "must be a non-negative integer", path + ".priority")
            priority = 5
        tags = _string_list(metadata, "tags", issues, path, required=False, nonempty=False)
        required_metadata = (
            name,
            rule_id,
            version,
            description,
            component,
            symptom,
            error_type,
            severity,
        )
        if all(item is not None for item in required_metadata):
            metadata_model = Metadata(
                name=name,
                id=rule_id,
                version=version,
                description=description,
                product_ids=products,
                sw_versions=software,
                component=component,
                symptom=symptom,
                error_type=error_type,
                severity=severity,
                priority=priority,
                tags=tags,
            )

    conditions = _require_mapping(signature.get("conditions"), issues, base_path + ".conditions")
    conditions_model = None
    if conditions is not None:
        path = base_path + ".conditions"
        logic = _require_string(conditions, "logic", issues, path)
        lookback = _require_integer(conditions, "logic_lookback_time", issues, path, minimum=0, maximum=86400)
        event_values = conditions.get("events")
        events = []
        if not isinstance(event_values, list) or not event_values:
            _issue(issues, "invalid_events", "must be a non-empty list", path + ".events")
        else:
            for event_index, item in enumerate(event_values):
                event = _validate_event(item, issues, "{}.events[{}]".format(path, event_index), registry)
                if event is not None:
                    events.append(event)
        ids = [event.id for event in events]
        if len(ids) != len(set(ids)):
            _issue(issues, "duplicate_event_id", "event IDs must be unique within a signature", path + ".events")
        tree = None
        if logic is not None:
            try:
                tree = parse_logic(logic, ids)
            except LogicSyntaxError as error:
                _issue(issues, "invalid_logic", str(error), path + ".logic")
        if tree is not None and lookback is not None:
            conditions_model = Conditions(
                logic=logic,
                logic_tree=tree,
                logic_lookback_time=lookback,
                events=tuple(events),
            )

    actions_model = _validate_actions(
        signature.get("actions"), issues, base_path + ".actions", default_timeout, registry
    )
    if metadata_model is None or conditions_model is None or actions_model is None or issues:
        return None, issues
    return Signature(
        metadata=metadata_model,
        conditions=conditions_model,
        actions=actions_model,
    ), issues


def _file_gate(document, supported_versions=SUPPORTED_SCHEMA_VERSIONS):
    issues = []
    if not isinstance(document, Mapping):
        _issue(issues, "invalid_top_level", "rules document must be an object", "$")
        return issues
    version = document.get("schema_version")
    if not isinstance(version, str):
        _issue(issues, "missing_schema_version", "schema_version must be a string", "$.schema_version")
    elif version not in supported_versions:
        _issue(
            issues,
            "unsupported_schema_version",
            "unsupported schema version {!r}".format(version),
            "$.schema_version",
        )
    signatures = document.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        _issue(issues, "invalid_signatures", "signatures must be a non-empty list", "$.signatures")
        return issues
    if len(signatures) > MAX_SIGNATURES:
        _issue(
            issues,
            "too_many_signatures",
            "signatures must contain at most {} entries".format(
                MAX_SIGNATURES
            ),
            "$.signatures",
        )
        return issues
    for index, wrapper in enumerate(signatures):
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("signature"), Mapping):
            _issue(
                issues,
                "invalid_signature_wrapper",
                "each list entry must contain a signature object",
                "$.signatures[{}]".format(index),
            )

    identities = {}
    names = {}
    for index, wrapper in enumerate(signatures):
        if not isinstance(wrapper, Mapping):
            continue
        signature = wrapper.get("signature")
        metadata = signature.get("metadata") if isinstance(signature, Mapping) else None
        if not isinstance(metadata, Mapping):
            continue
        rule_id = metadata.get("id")
        name = metadata.get("name")
        if _is_int(rule_id):
            if rule_id in identities:
                _issue(
                    issues,
                    "duplicate_rule_id",
                    "rule ID {} duplicates signatures[{}]".format(
                        rule_id, identities[rule_id]
                    ),
                    "$.signatures[{}].signature.metadata.id".format(index),
                )
            else:
                identities[rule_id] = index
        if isinstance(name, str):
            if name in names:
                _issue(
                    issues,
                    "duplicate_rule_name",
                    "rule name {!r} duplicates signatures[{}]".format(
                        name, names[name]
                    ),
                    "$.signatures[{}].signature.metadata.name".format(index),
                )
            else:
                names[name] = index
    return issues


def _to_file_issues(raw, source_lines=None):
    source_lines = source_lines or {}
    return tuple(
        ValidationIssue(
            scope="file",
            code=code,
            message=message,
            path=path,
            line=source_line_for_path(source_lines, path),
        )
        for code, message, path in raw
    )


def _rule_identity(raw, index):
    signature = raw.get("signature", {}) if isinstance(raw, Mapping) else {}
    metadata = signature.get("metadata", {}) if isinstance(signature, Mapping) else {}
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    rule_id = metadata.get("id") if isinstance(metadata, Mapping) else None
    version = metadata.get("version") if isinstance(metadata, Mapping) else None
    return (
        name if isinstance(name, str) else "signature[{}]".format(index),
        rule_id if _is_int(rule_id) else None,
        version if isinstance(version, str) else "",
    )


def _to_broken(raw, index, raw_issues, source_lines=None):
    source_lines = source_lines or {}
    name, rule_id, version = _rule_identity(raw, index)
    issues = tuple(
        ValidationIssue(
            scope="rule",
            code=code,
            message=message,
            path=path,
            rule_name=name,
            rule_id=rule_id,
            line=source_line_for_path(source_lines, path),
        )
        for code, message, path in raw_issues
    )
    return BrokenRule(
        rule_name=name,
        rule_id=rule_id,
        rule_version=version,
        issues=issues,
    )


def _context_for(signature, context, event_id=None):
    return DSEContext(
        product_id=context.product_id,
        software_version=context.software_version,
        component=signature.metadata.component,
        rule_name=signature.metadata.name,
        event_id=event_id,
    )


def _direct_sources(event):
    if not event.instances:
        return (ResolvedSource(type=event.type, path=event.path),)
    result = []
    for index, binding in enumerate(event.instances):
        instance, path_identifier = binding.split(":", 1)
        path = event.path
        if isinstance(path, Mapping):
            path = {
                key: (
                    item[index]
                    if isinstance(item, tuple) and key != "argv"
                    else item
                )
                for key, item in path.items()
            }
        vendor_data = (
            {"path_identifier": path_identifier} if path_identifier else {}
        )
        result.append(
            ResolvedSource(
                type=event.type,
                path=freeze_value(path),
                instance=instance,
                vendor_data=vendor_data,
            )
        )
    return tuple(result)


def _validate_resolved_source(source, event, registry):
    if source.type in registry.source_types:
        return
    raw = []
    _validate_source_path(source.type, source.path, (), raw, "resolved.path", registry)
    if raw:
        raise DSEError("; ".join(message for unused, message, unused_path in raw))
    if source.type in ("dse",):
        raise DSEError("DSE source must resolve to a concrete source type")


def materialize_signature(signature, context=None):
    """Resolve a validated signature into concrete monitor inputs.

    This function deliberately performs no hardware probing.  It validates
    bindings and hook contracts needed to construct a deterministic execution
    plan, which is the remote-activation requirement in schema 0.0.1.
    """

    context = context or ValidationContext()
    registry = context.dse_registry
    if context.require_compatibility_identity and not context.product_id:
        raise ValueError("current platform product identity is unavailable")
    if context.require_compatibility_identity and not context.software_version:
        raise ValueError("current platform software version is unavailable")
    if context.product_id is not None and not context.compatibility_matcher.product_matches(
        context.product_id, signature.metadata.product_ids
    ):
        raise ValueError("rule does not apply to product {!r}".format(context.product_id))
    if context.software_version is not None and not context.compatibility_matcher.software_matches(
        context.software_version, signature.metadata.sw_versions
    ):
        raise ValueError("rule does not apply to software {!r}".format(context.software_version))

    materialized = []
    for event in signature.conditions.events:
        dse_context = _context_for(signature, context, event.id)
        if event.type == "dse" or (
            event.type == "platform_api" and isinstance(event.path, str)
        ):
            sources = registry.resolve_source(event.path, dse_context)
        else:
            sources = _direct_sources(event)
        for source in sources:
            _validate_resolved_source(source, event, registry)
        for source in sources:
            if source.type in registry.source_types:
                if registry.hook is None:
                    raise DSEError("vendor source requires an installed DSE hook")
                registry.hook.validate_resolved_source(source, dse_context)
        materialized_event = event
        if event.evaluation.type == "dse":
            resolved_evaluation = registry.resolve_evaluation(
                event.evaluation.value,
                dse_context,
                rule_operator=event.evaluation.operator,
            )
            value_configs = event.evaluation.value_configs
            if value_configs == ValueConfig():
                value_configs = resolved_evaluation.value_configs
            evaluation = Evaluation(
                type="dse",
                value=resolved_evaluation.expected_value,
                operator=event.evaluation.operator or resolved_evaluation.operator,
                value_configs=value_configs,
                comparator=resolved_evaluation.comparator,
            )
            materialized_event = Event(
                id=event.id,
                type=event.type,
                path=event.path,
                evaluation=evaluation,
                match_count=event.match_count,
                match_period=event.match_period,
                instances=event.instances,
            )
        materialized.append(
            MaterializedEvent(event=materialized_event, sources=tuple(sources))
        )

    local = signature.actions.repair_actions.local_actions
    materialized_local = local
    if local is not None:
        local_operations = []
        for operation in local.action_list:
            dse_context = _context_for(signature, context)
            if operation.type == "dse":
                resolved = registry.resolve_action(operation.command, dse_context)
                options = dict(operation.options)
                options.update(dict(resolved.vendor_data))
                operation = Operation(
                    type=operation.type,
                    command=operation.command,
                    argv=operation.argv,
                    path=operation.path,
                    timeout=operation.timeout,
                    max_output_bytes=operation.max_output_bytes,
                    executor=resolved.executor,
                    options=options,
                )
            elif operation.type not in ("cli", "i2c"):
                registry.validate_vendor_operation(operation, dse_context, query=False)
            local_operations.append(operation)
        materialized_local = LocalActions(
            wait_period=local.wait_period,
            action_list=tuple(local_operations),
        )
    log_collection = signature.actions.log_collection
    materialized_log_collection = log_collection
    if log_collection is not None:
        queries = []
        for query in log_collection.queries:
            dse_context = _context_for(signature, context)
            if query.type == "dse":
                resolved = registry.resolve_query(query.command, dse_context)
                options = dict(query.options)
                options.update(dict(resolved.vendor_data))
                query = Operation(
                    type=query.type,
                    command=query.command,
                    argv=query.argv,
                    path=query.path,
                    timeout=query.timeout,
                    max_output_bytes=query.max_output_bytes,
                    executor=resolved.executor,
                    options=options,
                )
            elif query.type != "cli":
                registry.validate_vendor_operation(query, dse_context, query=True)
            queries.append(query)
        materialized_log_collection = LogCollection(
            logs=log_collection.logs,
            queries=tuple(queries),
        )
    materialized_signature = Signature(
        metadata=signature.metadata,
        conditions=signature.conditions,
        actions=Actions(
            repair_actions=RepairActions(
                remote_actions=signature.actions.repair_actions.remote_actions,
                local_actions=materialized_local,
            ),
            log_collection=materialized_log_collection,
        ),
    )
    return MaterializedRule(
        signature=materialized_signature, events=tuple(materialized)
    )


@dataclass(frozen=True)
class _RuntimeSchemaContract(object):
    """Trusted code handlers paired with one exact static schema version."""

    semantic_validator: object
    materializer: object


_RUNTIME_SCHEMA_CONTRACTS = MappingProxyType({
    "0.0.1": _RuntimeSchemaContract(
        semantic_validator=_validate_signature,
        materializer=materialize_signature,
    ),
})

if frozenset(DEFAULT_SCHEMA_REGISTRY.versions) != frozenset(
    _RUNTIME_SCHEMA_CONTRACTS
):
    raise SchemaRegistryError(
        "installed DLDD static and runtime schema versions do not match"
    )


def _require_runtime_schema_contract(version):
    try:
        return _RUNTIME_SCHEMA_CONTRACTS[version]
    except KeyError:
        # A packaged static schema without its code-side semantic and
        # materialization contract is an installation error, not bad vendor
        # input.  Fail closed instead of interpreting it as another version.
        raise SchemaRegistryError(
            "no DLDD runtime validation contract is installed for schema {}".format(
                version
            )
        )


def validate_document(
    document,
    context=None,
    materialize=True,
    source_lines=None,
    schema_registry=None,
):
    """Validate an already-parsed YAML/JSON rules document."""

    context = context or ValidationContext()
    source_lines = source_lines or {}
    version = (
        document.get("schema_version")
        if isinstance(document, Mapping)
        else None
    )
    try:
        _enforce_document_limits(document)
    except (RulesParseError, RecursionError, UnicodeError) as error:
        return ValidationResult(
            schema_version=version,
            ruleset=None,
            file_errors=_to_file_issues(
                (("parse_error", str(error), "$"),), source_lines
            ),
            source_lines=source_lines,
        )
    registry = schema_registry or DEFAULT_SCHEMA_REGISTRY
    file_issues = _file_gate(document, frozenset(registry.versions))
    if file_issues:
        return ValidationResult(
            schema_version=version,
            ruleset=None,
            file_errors=_to_file_issues(file_issues, source_lines),
            source_lines=source_lines,
        )
    default_timeout = document.get("local_action_default_timeout")
    timeout_file_issue = None
    if default_timeout is not None and (not _is_int(default_timeout) or default_timeout <= 0):
        timeout_file_issue = ValidationIssue(
            scope="file",
            code="invalid_default_timeout",
            message="local_action_default_timeout must be a positive integer",
            path="$.local_action_default_timeout",
            line=source_line_for_path(
                source_lines, "$.local_action_default_timeout"
            ),
        )
    if timeout_file_issue is not None:
        return ValidationResult(
            schema_version=version,
            ruleset=None,
            file_errors=(timeout_file_issue,),
            source_lines=source_lines,
        )

    contract = registry.require_exact(version)
    runtime_contract = _require_runtime_schema_contract(version)
    envelope_issues = contract.validate_envelope(document)
    if envelope_issues:
        raw_issues = tuple(
            (issue.code, issue.message, issue.path)
            for issue in envelope_issues
        )
        return ValidationResult(
            schema_version=version,
            ruleset=None,
            file_errors=_to_file_issues(raw_issues, source_lines),
            source_lines=source_lines,
        )

    signatures = []
    materialized = []
    broken = []
    for index, raw in enumerate(document["signatures"]):
        # Keep the long-standing semantic diagnostics (for example,
        # ``invalid_operator``) while also enforcing every constraint in the
        # versioned static schema.  The semantic pass is intentionally first:
        # its paths are more precise than a JSON Schema ``oneOf`` failure and
        # are part of the operator-facing telemetry contract.
        schema_issues = tuple(
            (issue.code, issue.message, issue.path)
            for issue in contract.validate_signature(raw, index)
        )
        signature, raw_issues = runtime_contract.semantic_validator(
            raw, index, default_timeout, context.dse_registry
        )
        combined_issues = tuple(raw_issues) + schema_issues
        if combined_issues or signature is None:
            broken.append(
                _to_broken(raw, index, combined_issues, source_lines)
            )
            continue
        if not materialize:
            signatures.append(signature)
            continue
        try:
            result = runtime_contract.materializer(signature, context)
        except ValueError as error:
            broken.append(
                _to_broken(
                    raw,
                    index,
                    (("materialization_failed", str(error), "$.signatures[{}].signature".format(index)),),
                    source_lines,
                )
            )
            continue
        signatures.append(signature)
        materialized.append(result)

    ruleset = RuleSet(
        schema_version=version,
        signatures=tuple(signatures),
        local_action_default_timeout=default_timeout,
    )
    return ValidationResult(
        schema_version=version,
        ruleset=ruleset,
        materialized_rules=tuple(materialized),
        broken_rules=tuple(broken),
        source_lines=source_lines,
    )


def _load_text(source):
    if hasattr(source, "read"):
        return _bounded_text(source.read(MAX_SOURCE_BYTES + 1))
    if isinstance(source, bytes):
        return _bounded_text(source)
    if isinstance(source, os.PathLike):
        with open(str(source), "rb") as stream:
            return _bounded_text(stream.read(MAX_SOURCE_BYTES + 1))
    if isinstance(source, str):
        try:
            is_file = os.path.isfile(source)
        except OSError:
            is_file = False
        if is_file:
            with open(source, "rb") as stream:
                return _bounded_text(stream.read(MAX_SOURCE_BYTES + 1))
        return _bounded_text(source)
    raise TypeError("source must be text, bytes, a path, or a readable stream")


def _parse_error_line(error):
    mark = getattr(error, "problem_mark", None) or getattr(
        error, "context_mark", None
    )
    if mark is not None:
        return mark.line + 1
    return getattr(error, "line", None) or getattr(error, "lineno", None)


def _parse_document_with_lines(source):
    text = _load_text(source)
    if not isinstance(text, str):
        raise TypeError("rules source must produce text")
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_nonfinite_json_number,
        )
    except json.JSONDecodeError as json_error:
        document = None
        json_error_line = json_error.lineno
    else:
        _enforce_document_limits(document)
        if yaml is None:
            return document, {"$": 1}
        # JSON is a YAML subset.  Compose it once solely to retain exact node
        # marks while json.loads remains authoritative for JSON scalar types.
        try:
            node = yaml.compose(text, Loader=_UniqueKeySafeLoader)
            return document, _build_source_lines(node)
        except Exception:
            return document, {"$": 1}

    if yaml is None:
        raise RulesParseError(
            "invalid JSON and PyYAML is unavailable",
            json_error_line,
        )

    _check_yaml_alias_limit(text)
    loader = _UniqueKeySafeLoader(text)
    try:
        node = loader.get_single_node()
        document = loader.construct_document(node) if node is not None else None
        _enforce_document_limits(document)
        source_lines = _build_source_lines(node)
        return document, source_lines or {"$": 1}
    except Exception as error:
        raise RulesParseError(
            "invalid YAML or JSON: {}".format(error),
            _parse_error_line(error),
        )
    finally:
        loader.dispose()


def load_document(source):
    """Safely parse YAML/JSON into primitive Python containers."""

    document, unused_source_lines = _parse_document_with_lines(source)
    return document


def load_rules(
    source, context=None, materialize=True, schema_registry=None
):
    """Parse and validate rules, returning failures as ``ValidationResult``."""

    try:
        document, source_lines = _parse_document_with_lines(source)
    except (
        ValueError,
        TypeError,
        UnicodeError,
        OSError,
        RecursionError,
        json.JSONDecodeError,
    ) as error:
        line = getattr(error, "line", None)
        if line is None and isinstance(error, UnicodeError):
            line = 1
        issue = ValidationIssue(
            scope="file",
            code="parse_error",
            message=str(error),
            path="$",
            line=line,
        )
        return ValidationResult(
            schema_version=None,
            ruleset=None,
            file_errors=(issue,),
            source_lines=({"$": line} if line is not None else {}),
        )
    return validate_document(
        document,
        context=context,
        materialize=materialize,
        source_lines=source_lines,
        schema_registry=schema_registry,
    )


# Explicit alias used by the activation pipeline.
validate_rules = validate_document
