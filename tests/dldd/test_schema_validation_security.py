from __future__ import absolute_import

import ast
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import pkgutil
from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from dldd.logic import MAX_LOGIC_NESTING
from dldd.planner import build_plans
from dldd.rule_schema import (
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    ContractRegistryError,
    RuleContract,
    normalize_validation_error,
)
from dldd.rule_schema.generate import (
    GENERATED_WARNING,
    JSON_SCHEMA_DIALECT,
    generate_schema,
    render_schema,
)
from dldd.rule_schema.v0_0_1 import (
    EnvelopeV001,
    MAX_REGEX_NESTING,
    RulesDocumentV001,
    SignatureWrapperV001,
    signature_v001_to_domain,
)
from dldd.validation import (
    MAX_COLLECTION_ITEMS,
    MAX_DOCUMENT_DEPTH,
    MAX_DOCUMENT_NODES,
    MAX_EVENTS_PER_SIGNATURE,
    MAX_ISSUES_PER_CANDIDATE,
    MAX_SCALAR_BYTES,
    MAX_SERIALIZED_DIAGNOSTIC_BYTES,
    MAX_SIGNATURES,
    MAX_SOURCE_BYTES,
    load_rules,
    validate_document,
)
from tests.dldd_fakes import load_valid_rules_document as _document


def _unique_rule_copy(document, name="SCHEMA_BAD", rule_id=1000002):
    wrapper = deepcopy(document["signatures"][0])
    metadata = wrapper["signature"]["metadata"]
    metadata["name"] = name
    metadata["id"] = rule_id
    return wrapper


def _file_unit_schema_violation(wrapper):
    event = wrapper["signature"]["conditions"]["events"][0]["event"]
    event["type"] = "file"
    event["path"] = {
        "file": "/tmp/dldd-fault",
        "format": "text",
        # The versioned Pydantic contract requires a non-empty unit when the
        # optional field is present.
        "unit": "",
    }


def test_generated_schema_and_exact_registry_dispatch_contract(
    monkeypatch,
):
    """Keep generated authoring schema derivative of exact runtime contracts."""

    packaged = pkgutil.get_data("dldd", "schemas/dld-rules-0.0.1.json")
    assert packaged is not None
    schema = json.loads(packaged.decode("utf-8"))
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")

    assert DEFAULT_CONTRACT_REGISTRY.versions == ("0.0.1",)
    assert schema == generate_schema("0.0.1")
    assert packaged.decode("utf-8") == render_schema("0.0.1")
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["x-generated-warning"] == GENERATED_WARNING
    assert schema["properties"]["schema_version"]["const"] == contract.version
    assert schema["properties"]["signatures"]["maxItems"] == MAX_SIGNATURES
    assert schema["$id"].endswith("/dld-rules-0.0.1.json")
    assert contract.validate_envelope(_document()).schema_version == "0.0.1"

    vendor = schema["$defs"]["VendorOperationV001"]
    assert vendor["properties"]["type"]["not"]["enum"] == [
        "cli",
        "dse",
        "i2c",
    ]
    reserved = {
        item["required"][0]
        for item in vendor["allOf"][0]["not"]["anyOf"]
    }
    assert reserved == {"argv", "command", "max_output_bytes", "path"}

    i2c_path = schema["$defs"]["I2CActionPathV001"]
    assert i2c_path["properties"]["value"] == {
        "$ref": "#/$defs/NonNullJsonValue"
    }
    assert all(
        option.get("type") != "null"
        for option in schema["$defs"]["NonNullJsonValue"]["anyOf"]
    )

    metadata = schema["$defs"]["MetadataV001"]
    component = metadata["properties"]["component"]
    assert component["type"] == "string"
    assert component["minLength"] == 1
    assert "enum" not in component
    assert "component" in metadata["required"]

    remote = schema["$defs"]["RemoteActionsV001"]
    remote_identity = remote["properties"]["action_list"]["items"]
    assert remote_identity["type"] == "string"
    assert remote_identity["minLength"] == 1
    assert "enum" not in remote_identity

    redis_event = schema["$defs"]["RedisEventV001"]
    assert "sampling_interval" not in redis_event["required"]
    assert redis_event["properties"]["sampling_interval"]["type"] == "integer"
    assert "local_action_default_timeout" not in schema["required"]
    assert schema["properties"]["local_action_default_timeout"][
        "type"
    ] == "integer"

    document = _document()

    def unexpected_schema_read(*unused_args, **unused_kwargs):
        pytest.fail("runtime validation read a generated schema artifact")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unexpected_schema_read)
        result = validate_document(document, materialize=False)

    assert result.file_valid
    assert len(result.ruleset.signatures) == 1

    # Dispatch accepts only an exact installed version string.
    for version in ("0.0.2", "0.0", "latest", "0.0.1 "):
        with pytest.raises(
            ContractRegistryError, match="unsupported schema_version"
        ):
            DEFAULT_CONTRACT_REGISTRY.require_exact(version)

        document = _document()
        document["schema_version"] = version
        result = validate_document(document, materialize=False)

        assert not result.file_valid
        assert [(issue.code, issue.path) for issue in result.file_errors] == [
            ("unsupported_schema_version", "$.schema_version")
        ]

    for version in (None, 1, [], {}):
        document = _document()
        document["schema_version"] = version
        result = validate_document(document, materialize=False)
        assert [(issue.code, issue.path) for issue in result.file_errors] == [
            ("missing_schema_version", "$.schema_version")
        ]

    # Registry provenance reaches every runtime object and rejects bad shapes.
    test_version = "9.9.9"

    class TestEnvelope(EnvelopeV001):
        schema_version: Literal["9.9.9"]

    class TestDocument(RulesDocumentV001):
        schema_version: Literal["9.9.9"]

    def to_test_domain(dto, **kwargs):
        return replace(
            signature_v001_to_domain(dto, **kwargs),
            schema_version=test_version,
        )

    contract = RuleContract(
        version=test_version,
        envelope=TypeAdapter(TestEnvelope),
        signature=TypeAdapter(SignatureWrapperV001),
        document=TypeAdapter(TestDocument),
        envelope_model=TestEnvelope,
        signature_model=SignatureWrapperV001,
        document_model=TestDocument,
        to_domain=to_test_domain,
    )
    registry = ContractRegistry({test_version: contract})
    document = _document()
    document["schema_version"] = test_version

    result = validate_document(document, contract_registry=registry)
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test-version",
        {"redis": 60, "file": 60, "common": 60},
    )

    assert result.activation_valid
    assert result.schema_version == test_version
    assert result.ruleset.signatures[0].schema_version == test_version
    assert result.materialized_rules[0].signature.schema_version == test_version
    assert {
        item.schema_version for item in bundle.work_items.values()
    } == {test_version}

    installed_versions = frozenset(DEFAULT_CONTRACT_REGISTRY.versions)
    package_root = Path(__file__).parents[2] / "dldd"
    offenders = []

    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root)
        if (
            relative.parts
            and relative.parts[0] == "rule_schema"
            and path.name.startswith("v")
        ):
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if (
                isinstance(node, ast.Constant)
                and node.value in installed_versions
            ):
                offenders.append("{}:{}".format(relative, node.lineno))

    assert offenders == []

    installed = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")

    with pytest.raises(ContractRegistryError, match="no DLDD rule contracts"):
        ContractRegistry({})
    with pytest.raises(ContractRegistryError, match="does not match version"):
        ContractRegistry({"0.0.2": installed})
    with pytest.raises(ContractRegistryError, match="model declares"):
        ContractRegistry(
            {"0.0.2": replace(installed, version="0.0.2")}
        )
    with pytest.raises(ContractRegistryError, match="no DTO-to-domain converter"):
        ContractRegistry({"0.0.1": replace(installed, to_domain=None)})
    with pytest.raises(ContractRegistryError, match="adapter does not match"):
        ContractRegistry(
            {"0.0.1": replace(installed, signature=TypeAdapter(int))}
        )


def test_file_and_rule_wire_errors_are_localized_and_bounded():
    """Localize envelope, signature, large integer, and runtime field errors."""

    document = _document()
    document["future_option"] = True

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert [(issue.code, issue.path) for issue in result.file_errors] == [
        ("unknown_field", "$.future_option")
    ]

    # Independent signature errors preserve the valid sibling rule.
    cases = (
        (
            lambda wrapper: wrapper["signature"]["metadata"].pop("severity"),
            "missing_field",
            "$.signatures[1].signature.metadata.severity",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["metadata"].update(
                {"description": date(2026, 7, 6)}
            ),
            "invalid_type",
            "$.signatures[1].signature.metadata.description",
            "yaml",
        ),
        (
            lambda wrapper: wrapper["signature"]["metadata"].update(
                {"future_option": "not-installed"}
            ),
            "unknown_field",
            "$.signatures[1].signature.metadata.future_option",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"future_option": "not-installed"}),
            "unknown_field",
            "$.signatures[1].signature.conditions.events[0].event.future_option",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["metadata"].update(
                {"id": "1000001"}
            ),
            "invalid_type",
            "$.signatures[1].signature.metadata.id",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"id": 1.0}),
            "invalid_type",
            "$.signatures[1].signature.conditions.events[0].event.id",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"match_count": True}),
            "invalid_type",
            "$.signatures[1].signature.conditions.events[0].event.match_count",
            "python",
        ),
        (
            _file_unit_schema_violation,
            "invalid_length",
            "$.signatures[1].signature.conditions.events[0].event.path.unit",
            "python",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"match_period": 3601}),
            "out_of_range",
            "$.signatures[1].signature.conditions.events[0].event.match_period",
            "python",
        ),
    )
    for mutate, expected_code, expected_path, source_format in cases:
        document = _document()
        bad = _unique_rule_copy(document)
        mutate(bad)
        document["signatures"].append(bad)

        if source_format == "yaml":
            yaml = pytest.importorskip("yaml")
            result = load_rules(yaml.safe_dump(document, sort_keys=False))
        else:
            result = validate_document(document)

        assert result.file_valid
        assert result.activation_valid
        assert len(result.usable_rules) == 1
        assert len(result.broken_rules) == 1
        assert [
            (issue.code, issue.path)
            for issue in result.broken_rules[0].issues
        ] == [(expected_code, expected_path)]

    # Huge YAML integers fail safely without formatting their full value.
    yaml = pytest.importorskip("yaml")
    document = _document()
    source = yaml.safe_dump(document, sort_keys=False).replace(
        "id: 1000001", "id: 0x" + "f" * 5000
    )

    result = load_rules(source)

    assert result.file_valid
    assert result.broken_rules[0].rule_id is None
    assert any(
        issue.code == "out_of_range"
        and issue.path == "$.signatures[0].signature.metadata.id"
        for issue in result.broken_rules[0].issues
    )

    # Every runtime-serialized integer field has an explicit safe bound.
    priority_document = _document()
    priority_document["signatures"][0]["signature"]["metadata"][
        "priority"
    ] = 2**32
    priority = validate_document(priority_document, materialize=False)
    assert any(
        issue.code == "out_of_range"
        and issue.path == "$.signatures[0].signature.metadata.priority"
        for issue in priority.broken_rules[0].issues
    )

    vendor_document = _document()
    action = vendor_document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update({"type": "acme_reset", "counter": 2**64})
    vendor = validate_document(vendor_document, materialize=False)
    assert any(
        issue.code == "out_of_range"
        and issue.path.endswith(".action.counter")
        for issue in vendor.broken_rules[0].issues
    )


def test_contract_defaults_omission_null_and_second_field_bounds():
    """Preserve defaults while distinguishing omission, null, and overflow."""

    document = _document()
    metadata = document["signatures"][0]["signature"]["metadata"]
    metadata.pop("priority", None)
    original = deepcopy(document)

    result = validate_document(document, materialize=False)

    assert document == original
    assert result.ruleset.signatures[0].metadata.priority == 5

    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    wrapper = deepcopy(_document()["signatures"][0])
    dto = contract.validate_signature(wrapper)
    event = dto.signature.conditions.events[0].event

    assert event.sampling_interval is None
    assert "sampling_interval" not in event.model_fields_set

    wrapper["signature"]["conditions"]["events"][0]["event"][
        "sampling_interval"
    ] = None
    with pytest.raises(ValidationError) as caught:
        contract.validate_signature(wrapper)

    assert [
        (issue.code, issue.message, issue.path)
        for issue in normalize_validation_error(
            caught.value, base_path="$.signatures[0]"
        )
    ] == [
        (
            "invalid_type",
            "value has the wrong type",
            "$.signatures[0].signature.conditions.events[0].event."
            "sampling_interval",
        )
    ]

    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    document = _document()
    document.pop("local_action_default_timeout")

    envelope = contract.validate_envelope(document)

    assert envelope.local_action_default_timeout is None
    assert "local_action_default_timeout" not in envelope.model_fields_set

    document["local_action_default_timeout"] = None
    with pytest.raises(ValidationError) as caught:
        contract.validate_envelope(document)
    assert [
        (issue.code, issue.path)
        for issue in normalize_validation_error(caught.value)
    ] == [("invalid_type", "$.local_action_default_timeout")]

    # All duration fields use the same unsigned 32-bit wire bound.
    for mutate, expected_path, file_scoped in (
        (
            lambda document: document.update(
                {"local_action_default_timeout": 2**32}
            ),
            "$.local_action_default_timeout",
            True,
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "actions"
            ]["repair_actions"]["local_actions"].update(
                {"wait_period": 2**32}
            ),
            "$.signatures[0].signature.actions.repair_actions.local_actions."
            "wait_period",
            False,
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "actions"
            ]["repair_actions"]["remote_actions"].update(
                {"time_window": 2**32}
            ),
            "$.signatures[0].signature.actions.repair_actions.remote_actions."
            "time_window",
            False,
        ),
    ):
        document = _document()
        mutate(document)

        result = validate_document(document)
        issues = (
            result.file_errors
            if file_scoped
            else result.broken_rules[0].issues
        )

        assert any(
            issue.code == "out_of_range" and issue.path == expected_path
            for issue in issues
        )


def test_vendor_operation_extension_and_canonical_error_paths():
    """Preserve vendor data while redacting values and normalizing key paths."""

    document = _document()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update(
        {
            "type": "acme_psu_reset",
            "target": "PSU0",
            "attempts": 2,
            "parameters": {"force": True},
        }
    )

    result = validate_document(document, materialize=False)
    operation = result.ruleset.signatures[0].actions.repair_actions
    operation = operation.local_actions.action_list[0]

    assert result.file_valid
    assert not result.broken_rules
    assert operation.type == "acme_psu_reset"
    assert dict(operation.options) == {
        "target": "PSU0",
        "attempts": 2,
        "parameters": {"force": True},
    }

    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    wrapper = deepcopy(_document()["signatures"][0])
    action = wrapper["signature"]["actions"]["repair_actions"][
        "local_actions"
    ]["action_list"][0]["action"]
    action.clear()
    action.update({"type": "acme_psu_reset", "payload": object()})

    with pytest.raises(ValidationError) as caught:
        contract.validate_signature(wrapper)
    issues = normalize_validation_error(
        caught.value, base_path="$.signatures[0]"
    )

    assert issues
    assert {issue.code for issue in issues} == {"invalid_type"}
    assert all("object at" not in issue.message for issue in issues)
    assert {issue.path for issue in issues} == {
        "$.signatures[0].signature.actions.repair_actions."
        "local_actions.action_list[0].action.payload"
    }


    # Keys resembling union branch internals retain canonical paths.
    for vendor_key in ("vendor", "cli", "dict[str,...]", "FooV001"):
        contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
        wrapper = deepcopy(_document()["signatures"][0])
        action = wrapper["signature"]["actions"]["repair_actions"][
            "local_actions"
        ]["action_list"][0]["action"]
        action.clear()
        action.update({"type": "acme_psu_reset", vendor_key: object()})

        with pytest.raises(ValidationError) as caught:
            contract.validate_signature(wrapper)
        issues = normalize_validation_error(
            caught.value, base_path="$.signatures[0]"
        )

        suffix = (
            ".{}".format(vendor_key)
            if vendor_key.replace("_", "a").isalnum()
            and not vendor_key[0].isdigit()
            else "[{}]".format(json.dumps(vendor_key))
        )
        assert [(issue.code, issue.path) for issue in issues] == [
            (
                "invalid_type",
                "$.signatures[0].signature.actions.repair_actions."
                "local_actions.action_list[0].action{}".format(suffix),
            )
        ]


    # Non-string mapping keys omit Pydantic's internal key marker.
    for mapping_key, path_suffix in (
        (1, "[1]"),
        (1.5, '["1.5"]'),
        (
            date(2026, 7, 6),
            '["datetime.date(2026, 7, 6)"]',
        ),
    ):
        contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
        wrapper = deepcopy(_document()["signatures"][0])
        action = wrapper["signature"]["actions"]["repair_actions"][
            "local_actions"
        ]["action_list"][0]["action"]
        action.clear()
        action.update(
            {"type": "acme_psu_reset", "payload": {mapping_key: "value"}}
        )

        with pytest.raises(ValidationError) as caught:
            contract.validate_signature(wrapper)
        issues = normalize_validation_error(
            caught.value, base_path="$.signatures[0]"
        )

        expected = (
            "$.signatures[0].signature.actions.repair_actions.local_actions."
            "action_list[0].action.payload{}".format(path_suffix)
        )
        assert any(issue.path == expected for issue in issues)
        assert all(".[key]" not in issue.path for issue in issues)

    # A literal '[key]' property remains escaped as user data.
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    wrapper = deepcopy(_document()["signatures"][0])
    action = wrapper["signature"]["actions"]["repair_actions"][
        "local_actions"
    ]["action_list"][0]["action"]
    action.clear()
    action.update(
        {"type": "acme_psu_reset", "payload": {"[key]": object()}}
    )

    with pytest.raises(ValidationError) as caught:
        contract.validate_signature(wrapper)
    issues = normalize_validation_error(
        caught.value, base_path="$.signatures[0]"
    )

    expected = (
        "$.signatures[0].signature.actions.repair_actions.local_actions."
        'action_list[0].action.payload["[key]"]'
    )
    assert any(issue.path == expected for issue in issues)


def test_diagnostic_redaction_bounding_and_identity_contract():
    """Keep normalized diagnostics distinct, deterministic, redacted, and bounded."""

    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    wrapper = deepcopy(_document()["signatures"][0])
    common_prefix = "x" * 3000
    wrapper["signature"]["metadata"][common_prefix + "A"] = True
    wrapper["signature"]["metadata"][common_prefix + "B"] = True

    with pytest.raises(ValidationError) as caught:
        contract.validate_signature(wrapper)
    issues = normalize_validation_error(
        caught.value, base_path="$.signatures[0]"
    )

    assert len(issues) == 2
    assert len({issue.path for issue in issues}) == 2
    assert all("...#" in issue.path for issue in issues)
    assert all(len(issue.path.encode("utf-8")) <= 2048 for issue in issues)

    # Many issues normalize deterministically without exposing input values.
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")
    wrapper = deepcopy(_document()["signatures"][0])
    wrapper["signature"]["metadata"].update(
        {
            "unknown_{:03d}".format(index): "DO-NOT-REPORT-{:03d}".format(
                index
            )
            for index in range(100)
        }
    )

    with pytest.raises(ValidationError) as caught:
        contract.validate_signature(wrapper)
    first = normalize_validation_error(
        caught.value, base_path="$.signatures[0]"
    )
    second = normalize_validation_error(
        caught.value, base_path="$.signatures[0]"
    )

    assert first == second
    assert len(first) == 65
    assert sum(
        issue.code == "validation_issues_truncated" for issue in first
    ) == 1
    assert sum(issue.code == "unknown_field" for issue in first) == 64
    assert all("DO-NOT-REPORT" not in issue.message for issue in first)
    assert first[-1].code == "validation_issues_truncated"
    assert first[:-1] == tuple(
        sorted(
            set(first[:-1]),
            key=lambda item: (item.path, item.code, item.message),
        )
    )

    # Candidate diagnostics apply aggregate issue, byte, and identity caps.
    document = _document()
    original = document["signatures"][0]
    signatures = []
    for index in range(64):
        wrapper = deepcopy(original)
        metadata = wrapper["signature"]["metadata"]
        metadata["id"] = 1_000_000 + index
        metadata["name"] = "RULE_{:03d}_{}".format(index, "X" * 120)
        metadata.update(
            {"unknown_{:03d}".format(item): item for item in range(64)}
        )
        signatures.append(wrapper)
    document["signatures"] = signatures

    result = validate_document(document, materialize=False)
    serialized = json.dumps(
        [asdict(item) for item in result.broken_rules],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(result.broken_rules) == 64
    assert sum(len(item.issues) for item in result.broken_rules) <= (
        MAX_ISSUES_PER_CANDIDATE
    )
    assert len(serialized) <= MAX_SERIALIZED_DIAGNOSTIC_BYTES
    assert any(
        issue.code == "validation_issues_truncated"
        for item in result.broken_rules
        for issue in item.issues
    )

    document = _document()
    original = document["signatures"][0]
    signatures = []
    for index in range(MAX_SIGNATURES):
        wrapper = deepcopy(original)
        metadata = wrapper["signature"]["metadata"]
        metadata["id"] = 1_000_000 + index
        metadata["name"] = "\0" * 240 + "{:04d}".format(index)
        metadata["unexpected"] = index
        signatures.append(wrapper)
    document["signatures"] = signatures
    source = json.dumps(document, separators=(",", ":"))

    assert len(source.encode("utf-8")) <= MAX_SOURCE_BYTES
    result = load_rules(source, materialize=False)
    serialized = json.dumps(
        [asdict(item) for item in result.broken_rules],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert result.file_valid
    assert len(result.broken_rules) == MAX_SIGNATURES
    assert len(serialized) <= MAX_SERIALIZED_DIAGNOSTIC_BYTES
    assert all(item.issues for item in result.broken_rules)
    assert sum(len(item.issues) for item in result.broken_rules) <= (
        MAX_ISSUES_PER_CANDIDATE
    )
    assert len({item.rule_name for item in result.broken_rules}) == MAX_SIGNATURES
    assert all("\0" not in item.rule_name for item in result.broken_rules)

    # Invalid metadata cannot become an unbounded or misleading rule identity.
    for mode, expected_rule_id in (("mapping", None), ("name", 1000001)):
        document = _document()
        metadata = document["signatures"][0]["signature"]["metadata"]
        if mode == "mapping":
            document["signatures"][0]["signature"]["metadata"] = []
        else:
            metadata["name"] = 123

        result = validate_document(document, materialize=False)

        assert result.file_valid
        assert result.broken_rules[0].rule_name == "signature[0]"
        assert result.broken_rules[0].rule_id == expected_rule_id
        assert result.broken_rules[0].issues[0].code == "invalid_type"


def test_expression_limits_and_stable_source_diagnostics():
    """Localize bounded logic/regex errors with stable paths and lines."""

    document = _document()
    conditions = document["signatures"][0]["signature"]["conditions"]
    conditions["logic"] = (
        "(" * (MAX_LOGIC_NESTING + 1)
        + "1"
        + ")" * (MAX_LOGIC_NESTING + 1)
    )

    result = validate_document(document)

    assert "invalid_logic" in {
        issue.code for issue in result.broken_rules[0].issues
    }

    document = _document()
    document["signatures"][0]["signature"]["conditions"]["logic"] = (
        "9" * 5000
    )

    result = validate_document(document)

    issue = result.broken_rules[0].issues[0]
    assert issue.code == "invalid_logic"
    assert issue.message == "logic expression is invalid"
    assert issue.path == "$.signatures[0].signature.conditions"

    document = _document()
    event = document["signatures"][0]["signature"]["conditions"]["events"][
        0
    ]["event"]
    event["evaluation"] = {
        "type": "string",
        "operator": "regex",
        "value": (
            "(" * (MAX_REGEX_NESTING + 1)
            + "a"
            + ")" * (MAX_REGEX_NESTING + 1)
        ),
    }

    result = validate_document(document)

    assert "invalid_regex" in {
        issue.code for issue in result.broken_rules[0].issues
    }

    # Repeated validation produces identical contract paths and source lines.
    document = _document()
    bad = _unique_rule_copy(document)
    _file_unit_schema_violation(bad)
    document["signatures"].append(bad)
    source = json.dumps(document, indent=2)
    expected_line = next(
        index
        for index, line in enumerate(source.splitlines(), 1)
        if '"unit": ""' in line
    )

    snapshots = []
    for unused in range(2):
        result = load_rules(source)
        issue = next(
            issue
            for issue in result.broken_rules[0].issues
            if issue.code == "invalid_length"
        )
        snapshots.append((issue.code, issue.path, issue.line))

    assert snapshots == [
        (
            "invalid_length",
            "$.signatures[1].signature.conditions.events[0].event.path.unit",
            expected_line,
        )
    ] * 2


def test_source_parsing_rejects_ambiguous_nonfinite_and_oversized_input():
    """Reject unsafe input before decode, construction, or contract dispatch."""

    for source, message, line in (
        (
            '{"schema_version":"0.0.1",'
            '"schema_version":"0.0.1","signatures":[]}',
            "duplicate JSON key 'schema_version'",
            None,
        ),
        (
            "schema_version: '0.0.1'\n"
            "schema_version: '0.0.1'\n"
            "signatures: []\n",
            "found duplicate key 'schema_version'",
            2,
        ),
    ):
        if source.startswith("schema_version"):
            pytest.importorskip("yaml")

        result = load_rules(source)
        issue = result.file_errors[0]
        assert issue.code == "parse_error"
        assert message in issue.message
        # JSON has no source offsets; YAML reports the second key line.
        assert issue.line == line

    # YAML construction rejects keys that cannot form a safe mapping.
    pytest.importorskip("yaml")
    result = load_rules(
        "schema_version: '0.0.1'\n"
        "? [not, hashable]\n"
        ": value\n"
        "signatures: []\n"
    )
    assert result.file_errors[0].code == "parse_error"
    assert "found unhashable key" in result.file_errors[0].message
    assert result.file_errors[0].line == 2

    # Nonfinite values and keys are rejected consistently across formats.
    for source in (
        '{"schema_version":"0.0.1","value":NaN,"signatures":[]}',
        "schema_version: '0.0.1'\nvalue: .inf\nsignatures: []\n",
        "schema_version: '0.0.1'\n.nan: value\nsignatures: []\n",
    ):
        result = load_rules(source)
        assert result.file_errors[0].code == "parse_error"
        assert "non-finite" in result.file_errors[0].message

    # Text source size is checked before parsing.
    result = load_rules(" " * (MAX_SOURCE_BYTES + 1))

    assert result.file_errors[0].code == "parse_error"
    assert "exceeds {} bytes".format(MAX_SOURCE_BYTES) in (
        result.file_errors[0].message
    )

    # Mapping keys use the same scalar-size guard as values.
    document = _document()
    document["k" * (MAX_SCALAR_BYTES + 1)] = True

    result = validate_document(document, materialize=False)

    assert result.file_errors[0].code == "parse_error"
    assert "scalar larger than {} bytes".format(MAX_SCALAR_BYTES) in (
        result.file_errors[0].message
    )

    # Byte and stream inputs are bounded before decoding.
    for source in (
        b" " * (MAX_SOURCE_BYTES + 1),
        BytesIO(b" " * (MAX_SOURCE_BYTES + 1)),
    ):
        result = load_rules(source)
        assert result.file_errors[0].code == "parse_error"
        assert "exceeds {} bytes".format(MAX_SOURCE_BYTES) in (
            result.file_errors[0].message
        )

    class InvalidStream(object):
        def read(self, unused_limit):
            return object()

    for source in (object(), InvalidStream(), b"\xff"):
        result = load_rules(source)
        assert result.file_errors[0].code == "parse_error"


def test_document_resource_alias_and_rule_count_limits():
    """Bound depth, nodes, collections, aliases, signatures, and events."""

    document = _document()
    nested = "leaf"
    for unused in range(MAX_DOCUMENT_DEPTH + 1):
        nested = {"nested": nested}
    document["vendor_extension"] = nested

    result = load_rules(json.dumps(document))

    assert result.file_errors[0].code == "parse_error"
    assert "nesting depth {}".format(MAX_DOCUMENT_DEPTH) in (
        result.file_errors[0].message
    )

    # Direct documents enforce scalar, collection, and total-node limits.
    for extension, expected in (
        ("x" * (MAX_SCALAR_BYTES + 1), "scalar larger"),
        (list(range(MAX_COLLECTION_ITEMS + 1)), "collection exceeds"),
        (
            [[None] * 9 for unused in range(MAX_COLLECTION_ITEMS)],
            "exceeds {} nodes".format(MAX_DOCUMENT_NODES),
        ),
    ):
        document = _document()
        document["vendor_extension"] = extension
        result = validate_document(document, materialize=False)
        assert not result.file_valid
        assert result.file_errors[0].code == "parse_error"
        assert expected in result.file_errors[0].message

    # Recursive direct objects and YAML aliases are rejected before models.
    document = _document()
    cycle = []
    cycle.append(cycle)
    document["vendor_extension"] = cycle

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert result.file_errors[0].code == "parse_error"
    assert "recursive aliases" in result.file_errors[0].message

    pytest.importorskip("yaml")
    source = (
        "schema_version: &version '0.0.1'\n"
        "copied_version: *version\n"
        "signatures: []\n"
    )

    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "YAML aliases are not allowed" in result.file_errors[0].message
    assert result.file_errors[0].line == 2

    # Signature overflow is file-scoped.
    document = _document()
    original = document["signatures"][0]
    signatures = []
    for index in range(MAX_SIGNATURES + 1):
        wrapper = deepcopy(original)
        metadata = wrapper["signature"]["metadata"]
        metadata["name"] = "RULE_{}".format(index)
        metadata["id"] = 1000000 + index
        signatures.append(wrapper)
    document["signatures"] = signatures

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert result.file_errors[0].code == "invalid_length"
    assert result.file_errors[0].path == "$.signatures"

    # Event overflow is localized to its rule in an otherwise usable file.
    document = _document()
    bad = _unique_rule_copy(document)
    conditions = bad["signature"]["conditions"]
    original_event = conditions["events"][0]
    conditions["events"] = [
        deepcopy(original_event) for unused in range(MAX_EVENTS_PER_SIGNATURE + 1)
    ]
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert any(
        issue.code == "invalid_length"
        and issue.path
        == "$.signatures[1].signature.conditions.events"
        for issue in result.broken_rules[0].issues
    )
