"""Versioned static, semantic, and materialization validation for DLDD rules."""

from __future__ import absolute_import

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field, replace
import json
import logging
import math
import os
from typing import Mapping, Optional

from pydantic import ValidationError

try:
    import yaml
except ImportError:  # pragma: no cover - SONiC images provide PyYAML
    yaml = None

from .dse import (
    DSEContext,
    DSEError,
    DSEEvaluationHandle,
    DSERegistry,
    DSESourceHandle,
    EMPTY_DSE_REGISTRY,
    parse_reference,
)
from .models import (
    BrokenRule,
    Evaluation,
    MaterializedEvent,
    MaterializedRule,
    Operation,
    ResolvedSource,
    RuleSet,
    ValidationIssue,
    ValidationResult,
    ValueConfig,
)
from .rule_schema import (
    ContractIssue,
    DEFAULT_CONTRACT_REGISTRY,
    DomainConversionError,
    normalize_validation_error,
)
from .rule_schema.errors import (
    append_path_component,
    bound_diagnostic,
    bound_identity,
    bound_path,
)


SUPPORTED_SCHEMA_VERSIONS = frozenset(DEFAULT_CONTRACT_REGISTRY.versions)

LOGGER = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_DOCUMENT_DEPTH = 64
MAX_DOCUMENT_NODES = 100000
MAX_COLLECTION_ITEMS = 10000
MAX_SCALAR_BYTES = 1024 * 1024
MAX_SIGNATURES = 1024
MAX_EVENTS_PER_SIGNATURE = 1000
MAX_YAML_ALIASES = 0
MAX_ISSUES_PER_CANDIDATE = 4096
MAX_SERIALIZED_DIAGNOSTIC_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_MESSAGE_BYTES = 1024
MAX_DIAGNOSTIC_PATH_BYTES = 2048
MAX_DIAGNOSTIC_IDENTITY_BYTES = 256
class CompatibilityMatcher(object, metaclass=ABCMeta):
    """Platform-owned matching contract for product and software versions."""

    @abstractmethod
    def product_matches(self, current_product, supported_products):
        raise NotImplementedError

    @abstractmethod
    def software_matches(self, current_version, supported_versions):
        raise NotImplementedError


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


def _enforce_scalar_limits(value):
    """Reject scalar values that exceed the bounded input contract."""

    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_SCALAR_BYTES:
        raise RulesParseError(
            "rules document contains a scalar larger than {} bytes".format(
                MAX_SCALAR_BYTES
            ),
            1,
        )
    if isinstance(value, bytes) and len(value) > MAX_SCALAR_BYTES:
        raise RulesParseError(
            "rules document contains a scalar larger than {} bytes".format(
                MAX_SCALAR_BYTES
            ),
            1,
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise RulesParseError("non-finite numbers are not allowed", 1)


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
        _enforce_scalar_limits(value)
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
        if isinstance(value, Mapping):
            # Keys are input scalars too, but they are not counted as value
            # nodes so the established document-node boundary is unchanged.
            for key, item in value.items():
                _enforce_scalar_limits(key)
                stack.append((item, depth + 1, nested))
        else:
            stack.extend((item, depth + 1, nested) for item in value)


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

    def visit(node, path):
        if node is None:
            return
        lines.setdefault(path, node.start_mark.line + 1)
        if isinstance(node, yaml.nodes.MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                if (
                    isinstance(key_node, yaml.nodes.ScalarNode)
                    and key_node.tag != "tag:yaml.org,2002:str"
                ):
                    key = yaml.safe_load(key_node.value)
                child_path = append_path_component(path, key)
                lines[child_path] = key_node.start_mark.line + 1
                visit(value_node, child_path)
        elif isinstance(node, yaml.nodes.SequenceNode):
            for index, item_node in enumerate(node.value):
                visit(
                    item_node,
                    append_path_component(path, index),
                )

    visit(root_node, "$")
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


def _is_rule_id(value):
    return _is_int(value) and 1_000_000 <= value <= 9_999_999


def _issue(issues, code, message, path):
    issues.append(ContractIssue(code=code, message=message, path=path))


def _file_gate(document, supported_versions=SUPPORTED_SCHEMA_VERSIONS):
    """Perform only the checks needed to select trusted model code.

    Pydantic owns the document and signature structure.  This small gate exists
    before model dispatch because an untrusted document cannot select anything
    except an exact, installed contract version.
    """

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
    return issues


def _duplicate_rule_identity_issues(signatures):
    """Enforce document-wide identity uniqueness after envelope validation."""

    issues = []
    identities = {}
    names = {}
    for index, wrapper in enumerate(signatures):
        signature = wrapper.get("signature")
        metadata = signature.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        rule_id = metadata.get("id")
        name = metadata.get("name")
        if _is_rule_id(rule_id):
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


def _bounded_issue(issue):
    return ContractIssue(
        code=bound_diagnostic(issue.code, 128),
        message=bound_diagnostic(
            issue.message, MAX_DIAGNOSTIC_MESSAGE_BYTES
        ),
        path=bound_path(issue.path, MAX_DIAGNOSTIC_PATH_BYTES),
    )


def _serialized_size(payload):
    return len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", "replace"
        )
    )


def _raw_issue_size(issue):
    return _serialized_size(
        {
            "scope": "file",
            "code": issue.code,
            "message": issue.message,
            "path": issue.path,
            "line": None,
        }
    )


def _truncation_issue(path):
    return ContractIssue(
        code="validation_issues_truncated",
        message="additional validation issues were omitted",
        path=bound_path(path, MAX_DIAGNOSTIC_PATH_BYTES),
    )


def _limit_file_issues(raw):
    marker = _truncation_issue("$")
    marker_size = _raw_issue_size(marker)
    selected = []
    used_bytes = 0
    for issue in raw:
        bounded = _bounded_issue(issue)
        size = _raw_issue_size(bounded)
        if (
            len(selected) + 1 >= MAX_ISSUES_PER_CANDIDATE
            or used_bytes + size + marker_size
            > MAX_SERIALIZED_DIAGNOSTIC_BYTES
        ):
            selected.append(marker)
            break
        selected.append(bounded)
        used_bytes += size
    return tuple(selected)


def _to_validation_issue(
    issue, scope, source_lines, rule_name=None, rule_id=None
):
    full_path = issue.path
    bounded = _bounded_issue(issue)
    return ValidationIssue(
        scope=scope,
        code=bounded.code,
        message=bounded.message,
        path=bounded.path,
        rule_name=rule_name,
        rule_id=rule_id,
        line=source_line_for_path(source_lines, full_path),
    )


def _to_file_issues(raw, source_lines=None):
    source_lines = source_lines or {}
    return tuple(
        _to_validation_issue(issue, "file", source_lines)
        for issue in _limit_file_issues(raw)
    )


def _file_failure(schema_version, raw_issues, source_lines, document=None):
    broken = []
    signatures = (
        document.get("signatures", ())
        if isinstance(document, Mapping)
        else ()
    )
    grouped = {}
    for issue in raw_issues:
        prefix = "$.signatures["
        if not issue.path.startswith(prefix):
            continue
        try:
            index = int(issue.path[len(prefix) :].split("]", 1)[0])
            raw = signatures[index]
        except (ValueError, IndexError, TypeError):
            continue
        grouped.setdefault(index, []).append(issue)
    for index, issues in sorted(grouped.items()):
        broken.append(
            _to_broken(
                _rule_identity(signatures[index], index), issues, source_lines
            )
        )
    return ValidationResult(
        schema_version=schema_version,
        ruleset=None,
        file_errors=_to_file_issues(raw_issues, source_lines),
        broken_rules=tuple(broken),
        source_lines=source_lines,
    )


def _rule_identity(raw, index):
    signature = raw.get("signature", {}) if isinstance(raw, Mapping) else {}
    metadata = signature.get("metadata", {}) if isinstance(signature, Mapping) else {}
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    rule_id = metadata.get("id") if isinstance(metadata, Mapping) else None
    version = metadata.get("version") if isinstance(metadata, Mapping) else None
    return (
        bound_identity(
            name if isinstance(name, str) else "signature[{}]".format(index),
            MAX_DIAGNOSTIC_IDENTITY_BYTES,
        ),
        rule_id if _is_rule_id(rule_id) else None,
        bound_identity(version if isinstance(version, str) else "", 64),
    )


def _to_broken(identity, raw_issues, source_lines=None):
    source_lines = source_lines or {}
    name, rule_id, version = identity
    return BrokenRule(
        rule_name=name,
        rule_id=rule_id,
        rule_version=version,
        issues=tuple(
            _to_validation_issue(
                issue,
                "rule",
                source_lines,
                rule_name=name,
                rule_id=rule_id,
            )
            for issue in raw_issues
        ),
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
    """Expand one Pydantic-validated direct event into source bindings.

    Every installed direct event contract has a mapping path. DSE references
    and the string form of Platform API paths are dispatched by
    :func:`materialize_signature` before this helper is called.
    """

    if not event.instances:
        return (ResolvedSource(type=event.type, path=event.path),)
    result = []
    for index, binding in enumerate(event.instances):
        instance, path_identifier = binding.split(":", 1)
        path = {
            key: (
                item[index]
                if isinstance(item, tuple) and key != "argv"
                else item
            )
            for key, item in event.path.items()
        }
        vendor_data = (
            {"path_identifier": path_identifier} if path_identifier else {}
        )
        result.append(
            ResolvedSource(
                type=event.type,
                path=path,
                instance=instance,
                vendor_data=vendor_data,
            )
        )
    return tuple(result)


def _materialize_operation(operation, registry, dse_context, *, query=False):
    """Resolve or validate one action/query through the shared DSE boundary."""

    if operation.type == "dse":
        resolver = registry.resolve_query if query else registry.resolve_action
        return operation.with_resolution(
            resolver(operation.command, dse_context)
        )
    builtin_types = ("cli",) if query else ("cli", "i2c")
    if operation.type not in builtin_types:
        registry.validate_vendor_operation(
            operation, dse_context, query=query
        )
    return operation


def materialize_signature(signature, context=None):
    """Resolve a validated signature into monitor inputs and DSE handles.

    This function deliberately performs no hardware probing.  It validates
    direct bindings and resolves DSE function references to callable handles.
    Runtime DSE expansion, collection, and evaluator reads remain owned by the
    monitor thread.
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
        dse_source_handle = None
        dse_source_reference = None
        if event.type == "dse" or (
            event.type == "platform_api" and isinstance(event.path, str)
        ):
            dse_source_reference = parse_reference(event.path)
            resolved_source = registry.resolve_source(event.path, dse_context)
            if isinstance(resolved_source, DSESourceHandle):
                dse_source_handle = resolved_source
                sources = ()
            else:
                sources = resolved_source
        else:
            sources = _direct_sources(event)
        for source in sources:
            if source.type in registry.source_types:
                if registry.hook is None:
                    raise DSEError("vendor source requires an installed DSE hook")
                registry.hook.validate_resolved_source(source, dse_context)
        materialized_event = event
        dse_evaluation_handle = None
        if event.evaluation.type == "dse":
            dse_evaluation_reference = parse_reference(
                event.evaluation.value
            )
            if (
                dse_source_reference is not None
                and dse_evaluation_reference.selector
                != dse_source_reference.selector
            ):
                LOGGER.warning(
                    "rule %r event %s explicitly maps DSE source selector "
                    "%r to evaluation selector %r; allowing this unusual "
                    "cross-selector mapping",
                    signature.metadata.name,
                    event.id,
                    dse_source_reference.selector,
                    dse_evaluation_reference.selector,
                )
            resolved_evaluation = registry.resolve_evaluation(
                event.evaluation.value,
                dse_context,
                rule_operator=event.evaluation.operator,
            )
            if isinstance(resolved_evaluation, DSEEvaluationHandle):
                dse_evaluation_handle = resolved_evaluation
                if (
                    dse_source_reference is None
                    and any(
                        token in resolved_evaluation.reference.selector
                        for token in ("*", "?")
                    )
                    and (
                        not sources
                        or any(source.instance is None for source in sources)
                    )
                ):
                    LOGGER.warning(
                        "rule %r event %s applies instanced DSE evaluation "
                        "selector %r to source bindings without explicit "
                        "instances; allowing this unusual evaluator mapping",
                        signature.metadata.name,
                        event.id,
                        resolved_evaluation.reference.selector,
                    )
            else:
                value_configs = event.evaluation.value_configs.with_fallback(
                    resolved_evaluation.value_configs
                )
                evaluation = Evaluation(
                    type="dse",
                    value=resolved_evaluation.expected_value,
                    operator=event.evaluation.operator or resolved_evaluation.operator,
                    value_configs=value_configs,
                    comparator=resolved_evaluation.comparator,
                )
                materialized_event = replace(event, evaluation=evaluation)
        materialized.append(
            MaterializedEvent(
                event=materialized_event,
                sources=tuple(sources),
                dse_context=dse_context,
                dse_source_handle=dse_source_handle,
                dse_evaluation_handle=dse_evaluation_handle,
            )
        )

    local = signature.actions.repair_actions.local_actions
    materialized_local = local
    operation_context = _context_for(signature, context)
    if local is not None:
        materialized_local = replace(
            local,
            action_list=tuple(
                _materialize_operation(
                    operation, registry, operation_context
                )
                for operation in local.action_list
            ),
        )
    log_collection = signature.actions.log_collection
    materialized_log_collection = log_collection
    if log_collection is not None:
        materialized_log_collection = replace(
            log_collection,
            queries=tuple(
                _materialize_operation(
                    query, registry, operation_context, query=True
                )
                for query in log_collection.queries
            ),
        )
    materialized_signature = replace(
        signature,
        actions=replace(
            signature.actions,
            repair_actions=replace(
                signature.actions.repair_actions,
                local_actions=materialized_local,
            ),
            log_collection=materialized_log_collection,
        ),
    )
    return MaterializedRule(
        signature=materialized_signature, events=tuple(materialized)
    )


def validate_document(
    document,
    context=None,
    materialize=True,
    source_lines=None,
    contract_registry=None,
):
    """Validate a parsed document with its exact Pydantic contract.

    Schema validation is atomic: one malformed signature rejects the file.
    Runtime collection/evaluation errors remain rule-local after activation.
    Generated JSON Schema files are deliberately not read by this path.
    """

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
        return _file_failure(
            version,
            (ContractIssue("parse_error", str(error), "$"),),
            source_lines,
        )
    registry = contract_registry or DEFAULT_CONTRACT_REGISTRY
    file_issues = _file_gate(document, frozenset(registry.versions))
    if file_issues:
        return _file_failure(version, file_issues, source_lines)

    contract = registry.require_exact(version)
    canonical_version = contract.version
    try:
        validated_document = contract.validate_document(document)
    except ValidationError as error:
        normalized = normalize_validation_error(error)
        return _file_failure(
            canonical_version, normalized, source_lines, document
        )

    identity_issues = _duplicate_rule_identity_issues(document["signatures"])
    if identity_issues:
        return _file_failure(
            canonical_version, identity_issues, source_lines
        )

    default_timeout = validated_document.local_action_default_timeout
    signatures = []
    materialized = []
    broken = []
    for index, (raw, dto) in enumerate(
        zip(document["signatures"], validated_document.signatures)
    ):
        base_path = "$.signatures[{}]".format(index)
        try:
            signature = contract.to_domain(
                dto, local_action_default_timeout=default_timeout
            )
        except DomainConversionError as error:
            relative_path = (
                error.path[1:] if error.path.startswith("$") else error.path
            )
            return _file_failure(
                canonical_version,
                (
                    ContractIssue(
                        code=error.code,
                        message=error.message,
                        path=base_path + relative_path,
                    ),
                ),
                source_lines,
                document,
            )

        if not materialize:
            signatures.append(signature)
            continue
        try:
            result = materialize_signature(signature, context)
        except DSEError as error:
            return _file_failure(
                canonical_version,
                (
                    ContractIssue(
                        code="dse_hook_unresolved",
                        message=str(error),
                        path=base_path + ".signature",
                    ),
                ),
                source_lines,
                document,
            )
        except ValueError as error:
            broken.append(
                _to_broken(
                    _rule_identity(raw, index),
                    (
                        ContractIssue(
                            code="materialization_failed",
                            message=str(error),
                            path=base_path + ".signature",
                        ),
                    ), source_lines,
                )
            )
            continue
        signatures.append(signature)
        materialized.append(result)

    ruleset = RuleSet(
        schema_version=canonical_version,
        signatures=tuple(signatures),
        local_action_default_timeout=default_timeout,
    )
    return ValidationResult(
        schema_version=canonical_version,
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
    source, context=None, materialize=True, contract_registry=None
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
        contract_registry=contract_registry,
    )


# Explicit alias used by the activation pipeline.
validate_rules = validate_document
