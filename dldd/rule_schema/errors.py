"""Stable DLDD diagnostics for Pydantic contract failures."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable, Tuple

from pydantic import ValidationError


DEFAULT_MAX_ISSUES = 64
DEFAULT_MAX_MESSAGE_BYTES = 1024
DEFAULT_MAX_PATH_BYTES = 2048


@dataclass(frozen=True)
class ContractIssue:
    code: str
    message: str
    path: str


class DomainConversionError(ValueError):
    """Expected rule-local failure converting a validated DTO to a domain rule."""

    def __init__(self, code: str, message: str, path: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


_TYPE_CODES = {
    "missing": "missing_field",
    "dict_type": "invalid_type",
    "list_type": "invalid_type",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "bool_type": "invalid_type",
    "float_type": "invalid_type",
    "model_type": "invalid_type",
    "mapping_type": "invalid_type",
    "extra_forbidden": "unknown_field",
    "literal_error": "unsupported_value",
    "enum": "unsupported_value",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "finite_number": "invalid_value",
    "too_short": "invalid_length",
    "too_long": "invalid_length",
    "string_too_short": "invalid_length",
    "string_too_long": "invalid_length",
    "list_too_short": "invalid_length",
    "list_too_long": "invalid_length",
    "string_pattern_mismatch": "invalid_format",
    "union_tag_not_found": "unsupported_type",
    "union_tag_invalid": "unsupported_type",
}

_MESSAGES = {
    "missing_field": "field is required",
    "invalid_type": "value has the wrong type",
    "unknown_field": "unknown field is not permitted",
    "unsupported_value": "value is not permitted",
    "out_of_range": "value is outside the permitted range",
    "invalid_length": "value has an invalid length",
    "invalid_format": "value has an invalid format",
    "unsupported_type": "type is missing or unsupported",
    "invalid_value": "value is not valid",
}

_CUSTOM_CODES = frozenset(
    (
        "duplicate_event_id",
        "duplicate_instance",
        "empty_log_collection",
        "instance_path_mismatch",
        "instance_value_mismatch",
        "invalid_logic",
        "invalid_mask_value",
        "invalid_match_window",
        "invalid_regex",
        "missing_i2c_value",
        "reserved_operation_field",
        "reserved_operation_type",
    )
)
_CUSTOM_MESSAGES = {
    "duplicate_event_id": "event IDs must be unique within a signature",
    "duplicate_instance": "component instances must be unique",
    "empty_log_collection": "at least one log or query is required",
    "instance_path_mismatch": "instances and source paths do not align",
    "instance_value_mismatch": "instances and comparison values do not align",
    "invalid_logic": "logic expression is invalid",
    "invalid_mask_value": "mask value is not a valid integer",
    "invalid_match_window": "match window is not valid",
    "invalid_regex": "regular expression is not valid",
    "missing_i2c_value": "I2C set action requires a value",
    "reserved_operation_field": "vendor operation uses a reserved field",
    "reserved_operation_type": "built-in operation must use its built-in contract",
}

_UNION_OWNERS = frozenset(("event", "evaluation", "action", "query"))
_UNION_TAGS = frozenset(
    (
        "boolean",
        "cli",
        "comparison",
        "dse",
        "file",
        "i2c",
        "mask",
        "platform_api",
        "redis",
        "string",
        "sysfs",
        "vendor",
    )
)
_SCALAR_UNION_BRANCHES = frozenset(
    (
        "bool",
        "bytes",
        "constrained-float",
        "constrained-int",
        "constrained-str",
        "float",
        "int",
        "none",
        "str",
    )
)
_MODEL_UNION_BRANCHES = frozenset(("PlatformAPIHookPathV001",))
_UNION_VALUE_FIELDS = frozenset(
    ("bus", "database", "key", "path", "scaling", "table", "value")
)
_VENDOR_STANDARD_FIELDS = frozenset(("hook", "timeout", "type"))
_PATH_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_container_union_branch(part: str) -> bool:
    return part.startswith(
        (
            "dict[",
            "function-",
            "list[",
            "literal[",
            "nullable[",
            "set[",
            "tuple[",
            "union[",
        )
    )


def bound_diagnostic(value: str, maximum: int) -> str:
    raw = str(value).encode("utf-8", "replace")
    if len(raw) <= maximum:
        return raw.decode("utf-8")
    suffix = b"..."
    return (raw[: max(0, maximum - len(suffix))] + suffix).decode(
        "utf-8", "ignore"
    )


def bound_path(value: str, maximum: int = DEFAULT_MAX_PATH_BYTES) -> str:
    """Bound a wire path while retaining deterministic collision resistance."""

    raw = str(value).encode("utf-8", "replace")
    if len(raw) <= maximum:
        return raw.decode("utf-8")
    suffix = ("...#" + hashlib.sha256(raw).hexdigest()[:16]).encode("ascii")
    return (raw[: max(0, maximum - len(suffix))] + suffix).decode(
        "utf-8", "ignore"
    )


def bound_identity(value: str, maximum: int = 256) -> str:
    """Return a compact, JSON-safe diagnostic identity.

    Rule names remain unrestricted contract strings, so they may legally
    contain control characters or Unicode whose JSON-escaped representation
    is much larger than its UTF-8 input.  Diagnostics use a printable ASCII
    projection and a hash whenever that projection changes or is truncated.
    This keeps external byte limits enforceable without conflating distinct
    hostile identities.
    """

    text = str(value)
    raw = text.encode("utf-8", "replace")
    projected = "".join(
        character
        if 0x20 <= ord(character) <= 0x7E and character not in ('"', "\\")
        else "_"
        for character in text
    )
    encoded = projected.encode("ascii")
    if projected == text and len(encoded) <= maximum:
        return projected
    suffix = ("...#" + hashlib.sha256(raw).hexdigest()[:16]).encode("ascii")
    return (encoded[: max(0, maximum - len(suffix))] + suffix).decode("ascii")


def _wire_location(parts: Iterable[object]) -> Tuple[object, ...]:
    parts = tuple(parts)
    result = []
    force_wire_component = False
    forced_container = None
    skip_mapping_key_marker = False
    nested_json_value = False
    vendor_payload = False
    expect_union_branch = False
    for index, part in enumerate(parts):
        # Pydantic appends a synthetic ``[key]`` location after a mapping key
        # that failed ``dict[str, ...]`` key validation.  A real vendor key
        # whose spelling is ``[key]`` does not enter this state and is kept.
        if skip_mapping_key_marker and part == "[key]":
            skip_mapping_key_marker = False
            continue
        skip_mapping_key_marker = False

        if force_wire_component:
            result.append(part)
            if forced_container == "dict":
                skip_mapping_key_marker = True
            force_wire_component = False
            forced_container = None
            expect_union_branch = (
                nested_json_value
                or part in _UNION_VALUE_FIELDS
                or (
                    vendor_payload
                    and part not in _VENDOR_STANDARD_FIELDS
                )
            )
            nested_json_value = False
            continue

        # Tagged unions include their selected branch between the owning wire
        # field and the next real field.  Branch names are implementation
        # details and must not leak into operator-visible JSON paths.
        if (
            isinstance(part, str)
            and part in _UNION_TAGS
            and index > 0
            and parts[index - 1] in _UNION_OWNERS
        ):
            if part == "vendor":
                vendor_payload = True
            force_wire_component = True
            continue

        # A union branch is recognized only when the preceding wire field says
        # one is expected.  After a container branch, exactly one following
        # component is forced to be a real dict key/list index.  This state is
        # what preserves legal vendor keys such as ``cli``, ``vendor``, or even
        # ``dict[str,...]`` instead of guessing from their spelling.
        if expect_union_branch and isinstance(part, str):
            if part in _SCALAR_UNION_BRANCHES:
                expect_union_branch = False
                continue
            if part in _MODEL_UNION_BRANCHES:
                expect_union_branch = False
                vendor_payload = True
                force_wire_component = True
                continue
            if _is_container_union_branch(part):
                expect_union_branch = False
                nested_json_value = True
                force_wire_component = True
                forced_container = (
                    "dict" if part.startswith("dict[") else "sequence"
                )
                continue
            expect_union_branch = False

        result.append(part)
        expect_union_branch = (
            part in _UNION_VALUE_FIELDS
            or (
                vendor_payload
                and part not in _VENDOR_STANDARD_FIELDS
            )
        )
    return tuple(result)


def append_path_component(path: str, part: object) -> str:
    """Append one property/index using DLDD's canonical JSON-style syntax."""

    if isinstance(part, int) and not isinstance(part, bool):
        return path + "[{}]".format(part)
    if isinstance(part, bool):
        # Python mappings canonicalize bool keys as their equal integer key;
        # Pydantic reports the same integer location.
        return path + "[{}]".format(int(part))
    if isinstance(part, str) and _PATH_IDENTIFIER.match(part):
        return path + ".{}".format(part)
    if not isinstance(part, str):
        # Pydantic uses repr-like strings for non-string mapping keys in its
        # location tuple.  Mirror that representation in the source-line map.
        part = repr(part)
    return path + "[{}]".format(
        json.dumps(str(part), ensure_ascii=True, separators=(",", ":"))
    )


def _json_path(base_path: str, parts: Iterable[object]) -> str:
    path = base_path
    for part in _wire_location(parts):
        path = append_path_component(path, part)
    return path


def normalize_validation_error(
    error: ValidationError,
    *,
    base_path: str = "$",
    max_issues: int = DEFAULT_MAX_ISSUES,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    max_path_bytes: int = DEFAULT_MAX_PATH_BYTES,
) -> Tuple[ContractIssue, ...]:
    """Convert a Pydantic failure to deterministic, redacted DLDD issues."""

    normalized = []
    raw_errors = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    for item in raw_errors[:max_issues]:
        error_type = str(item.get("type", "invalid_value"))
        if error_type in _CUSTOM_CODES:
            code = error_type
            message = _CUSTOM_MESSAGES[code]
        else:
            code = _TYPE_CODES.get(error_type, "invalid_value")
            message = _MESSAGES[code]
        normalized.append(
            ContractIssue(
                code=code,
                message=bound_diagnostic(message, max_message_bytes),
                path=bound_path(
                    _json_path(base_path, item.get("loc", ())), max_path_bytes
                ),
            )
        )

    result = tuple(
        sorted(set(normalized), key=lambda issue: (issue.path, issue.code, issue.message))
    )
    if len(raw_errors) > max_issues:
        result += (
            ContractIssue(
                code="validation_issues_truncated",
                message="additional validation issues were omitted",
                path=base_path,
            ),
        )
    return result


__all__ = (
    "ContractIssue",
    "DomainConversionError",
    "append_path_component",
    "bound_diagnostic",
    "bound_identity",
    "bound_path",
    "normalize_validation_error",
)
