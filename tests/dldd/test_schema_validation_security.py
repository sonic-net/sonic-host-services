from __future__ import absolute_import

from copy import deepcopy
from io import BytesIO
import json
import os
from pathlib import Path
import pkgutil

import pytest

from dldd.lifecycle import (
    CandidateValidation,
    RuleGenerationManager,
    RulePaths,
    sha256_file,
)
from dldd.logic import MAX_LOGIC_NESTING
from dldd.schema_registry import (
    DEFAULT_SCHEMA_REGISTRY,
    DRAFT7_URI,
    SchemaRegistry,
    SchemaRegistryError,
)
from dldd.validation import (
    MAX_COLLECTION_ITEMS,
    MAX_DOCUMENT_DEPTH,
    MAX_DOCUMENT_NODES,
    MAX_EVENTS_PER_SIGNATURE,
    MAX_REGEX_NESTING,
    MAX_SCALAR_BYTES,
    MAX_SIGNATURES,
    MAX_SOURCE_BYTES,
    load_rules,
    validate_document,
)


FIXTURES = Path(__file__).parent / "fixtures"
RULE_SCHEMA = (
    Path(__file__).parents[2] / "dldd" / "schemas" / "dld-rules-0.0.1.json"
)


def _document():
    return json.loads((FIXTURES / "valid-redis-rule.json").read_text())


def _write_schema(tmp_path, version="0.0.1", mutate=None, name=None):
    schema = json.loads(RULE_SCHEMA.read_text())
    schema["properties"]["schema_version"]["const"] = version
    schema["$id"] = "https://sonic-net.github.io/dldd/schemas/dld-rules-{}.json".format(
        version
    )
    if mutate is not None:
        mutate(schema)
    path = tmp_path / (name or "dld-rules-{}.json".format(version))
    path.write_text(json.dumps(schema))
    return path


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
        # The hand-written semantic validator historically accepted this;
        # the versioned schema deliberately requires a non-empty unit.
        "unit": "",
    }


def _candidate_validation(path, unused_dse):
    result = load_rules(path)
    broken = tuple(
        {
            "rule": item.rule_name,
            "rule_id": item.rule_id,
            "reason": "; ".join(str(issue) for issue in item.issues),
        }
        for item in result.broken_rules
    )
    errors = tuple(str(issue) for issue in result.file_errors)
    if not result.materialized_rules:
        errors += tuple(item["reason"] for item in broken)
    return CandidateValidation(
        file_valid=result.file_valid,
        usable_rule_count=len(result.materialized_rules),
        schema_version=result.schema_version or "",
        broken_rules=broken,
        errors=errors,
        payload=result,
    )


def _lifecycle_paths(tmp_path):
    platform = tmp_path / "platform"
    platform.mkdir()
    return RulePaths(
        str(platform),
        inbox=str(tmp_path / "inbox" / "dld_rules.yaml"),
        rules_dir=str(tmp_path / "rules"),
        state_file=str(tmp_path / "state.json"),
    )


def _accept_inbox(paths):
    os.makedirs(os.path.dirname(paths.watcher_state), exist_ok=True)
    Path(paths.watcher_state).write_text(
        json.dumps({"last_restart_checksum": sha256_file(paths.inbox)})
    )


def test_packaged_registry_contract_is_draft7_and_executable():
    packaged = pkgutil.get_data(
        "dldd", "schemas/dld-rules-0.0.1.json"
    )
    assert packaged is not None
    schema = json.loads(packaged.decode("utf-8"))
    contract = DEFAULT_SCHEMA_REGISTRY.require_exact("0.0.1")

    assert DEFAULT_SCHEMA_REGISTRY.versions == ("0.0.1",)
    assert schema["$schema"] == DRAFT7_URI
    assert schema["properties"]["schema_version"]["const"] == contract.version
    assert schema["properties"]["signatures"]["maxItems"] == MAX_SIGNATURES
    assert (
        schema["definitions"]["conditions"]["properties"]["events"][
            "maxItems"
        ]
        == MAX_EVENTS_PER_SIGNATURE
    )
    assert schema["$id"] == contract.schema_id
    assert Path(contract.schema_path).read_bytes() == packaged
    assert contract.layout_path is not None
    assert Path(contract.layout_path).is_file()
    assert pkgutil.get_data(
        "dldd", "schemas/schema-layout-0.0.1.json"
    ) == Path(contract.layout_path).read_bytes()

    # Exercise Draft-7 if/then and const, rather than merely parsing the JSON.
    wrapper = deepcopy(_document()["signatures"][0])
    event = wrapper["signature"]["conditions"]["events"][0]["event"]
    event["type"] = "i2c"
    event["path"] = {
        "bus": "1",
        "chip_addr": "0x58",
        "i2c_type": "set",
        "command": "0x7A",
        "size": "b",
    }
    issues = contract.validate_signature(wrapper, 0)

    assert len(issues) == 1
    assert issues[0].code == "schema_const"
    assert issues[0].path.endswith(".path.i2c_type")


def test_registry_dispatches_only_exact_schema_versions(tmp_path):
    with pytest.raises(SchemaRegistryError, match="unsupported schema_version"):
        DEFAULT_SCHEMA_REGISTRY.require_exact("0.0.2")

    schema_path = _write_schema(tmp_path, version="0.0.2")
    registry = SchemaRegistry({"0.0.2": str(schema_path)})
    document = _document()
    document["schema_version"] = "0.0.2"

    # A static contract without its exact code-side semantic/materialization
    # handler is an installation failure, not permission to reinterpret it as
    # 0.0.1.
    with pytest.raises(SchemaRegistryError, match="no DLDD runtime"):
        validate_document(
            document, materialize=False, schema_registry=registry
        )

    assert registry.versions == ("0.0.2",)


@pytest.mark.parametrize(
    "reference",
    (
        "https://example.invalid/schema.json",
        "file:///etc/passwd",
        "//example.invalid/schema.json",
        "../other.json#/definitions/value",
    ),
)
def test_registry_rejects_every_nonlocal_reference(tmp_path, reference):
    def add_reference(schema):
        schema["definitions"]["external_test"] = {"$ref": reference}

    schema_path = _write_schema(tmp_path, mutate=add_reference)

    with pytest.raises(SchemaRegistryError, match="non-local reference"):
        SchemaRegistry({"0.0.1": str(schema_path)})


@pytest.mark.parametrize(
    "mutation, expected",
    (
        (lambda schema: schema.update({"$schema": "draft-unknown"}), "Draft 7"),
        (lambda schema: schema.pop("$id"), "non-empty \\$id"),
        (
            lambda schema: schema["properties"]["schema_version"].update(
                {"const": "9.9.9"}
            ),
            "does not match schema const",
        ),
    ),
)
def test_registry_rejects_corrupt_installed_contracts(
    tmp_path, mutation, expected
):
    schema_path = _write_schema(tmp_path, mutate=mutation)

    with pytest.raises(SchemaRegistryError, match=expected):
        SchemaRegistry({"0.0.1": str(schema_path)})


def test_registry_rejects_missing_resource(tmp_path):
    with pytest.raises(SchemaRegistryError, match="unable to load"):
        SchemaRegistry({"0.0.1": str(tmp_path / "missing.json")})


def test_registry_rejects_duplicate_keys_in_installed_schema(tmp_path):
    source = RULE_SCHEMA.read_text()
    source = source.replace(
        '"$schema": "http://json-schema.org/draft-07/schema#",',
        '"$schema": "ignored",\n'
        '  "$schema": "http://json-schema.org/draft-07/schema#",',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(source)

    with pytest.raises(SchemaRegistryError, match="duplicate key.*schema"):
        SchemaRegistry({"0.0.1": str(path)})


def test_optional_layout_is_not_a_daemon_startup_dependency(tmp_path):
    schema_path = _write_schema(tmp_path)
    missing_layout = tmp_path / "missing-layout.json"

    contract = SchemaRegistry(
        {"0.0.1": str(schema_path)},
        {"0.0.1": str(missing_layout)},
    ).require_exact("0.0.1")

    assert contract.layout_path == str(missing_layout)


def test_envelope_preserves_version_specific_top_level_requirements(tmp_path):
    def require_vendor_header(schema):
        schema["required"].append("vendor_header")
        schema["properties"]["vendor_header"] = {"type": "string"}

    schema_path = _write_schema(tmp_path, mutate=require_vendor_header)
    contract = SchemaRegistry({"0.0.1": str(schema_path)}).require_exact(
        "0.0.1"
    )

    issues = contract.validate_envelope(_document())

    assert [(issue.code, issue.path) for issue in issues] == [
        ("schema_required", "$.vendor_header")
    ]


def test_registry_rejects_unresolved_local_reference_without_network(
    tmp_path, monkeypatch
):
    def add_bad_reference(schema):
        schema["definitions"]["bad"] = {
            "$ref": "#/definitions/does_not_exist"
        }

    def unexpected_network(*unused_args, **unused_kwargs):
        pytest.fail("schema validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_network)
    schema_path = _write_schema(tmp_path, mutate=add_bad_reference)

    with pytest.raises(SchemaRegistryError, match="Unresolvable ref"):
        SchemaRegistry({"0.0.1": str(schema_path)})


def test_registry_rejects_nested_schema_identifiers(tmp_path):
    def add_nested_id(schema):
        schema["definitions"]["bad"] = {
            "$id": "https://example.invalid/nested.json",
            "type": "string",
        }

    schema_path = _write_schema(tmp_path, mutate=add_nested_id)

    with pytest.raises(SchemaRegistryError, match="nested \\$id"):
        SchemaRegistry({"0.0.1": str(schema_path)})


def test_schema_required_error_is_rule_scoped_in_mixed_generation():
    document = _document()
    bad = _unique_rule_copy(document)
    del bad["signature"]["metadata"]["severity"]
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert len(result.broken_rules) == 1
    schema_issue = next(
        issue
        for issue in result.broken_rules[0].issues
        if issue.code == "schema_required"
    )
    assert schema_issue.path == (
        "$.signatures[1].signature.metadata.severity"
    )


def test_schema_only_constraint_is_enforced_per_rule():
    document = _document()
    bad = _unique_rule_copy(document)
    _file_unit_schema_violation(bad)
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert len(result.broken_rules) == 1
    assert [
        (issue.code, issue.path)
        for issue in result.broken_rules[0].issues
        if issue.code.startswith("schema_")
    ] == [
        (
            "schema_minLength",
            "$.signatures[1].signature.conditions.events[0].event.path.unit",
        )
    ]


def test_static_schema_adds_coverage_without_replacing_semantic_issue():
    document = _document()
    event = document["signatures"][0]["signature"]["conditions"]["events"][
        0
    ]["event"]
    event["match_period"] = 3601

    result = validate_document(document)

    assert [issue.code for issue in result.broken_rules[0].issues] == [
        "out_of_range",
        "schema_maximum",
    ]


def test_schema_defaults_do_not_mutate_input():
    document = _document()
    metadata = document["signatures"][0]["signature"]["metadata"]
    metadata.pop("priority", None)
    original = deepcopy(document)

    result = validate_document(document, materialize=False)

    assert document == original
    assert result.ruleset.signatures[0].metadata.priority == 5


def test_excessively_nested_logic_is_a_rule_diagnostic():
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


def test_excessively_nested_regex_is_a_rule_diagnostic():
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


def test_schema_issue_path_and_line_are_stable_across_runs():
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
            if issue.code == "schema_minLength"
        )
        snapshots.append((issue.code, issue.path, issue.line))

    assert snapshots == [
        (
            "schema_minLength",
            "$.signatures[1].signature.conditions.events[0].event.path.unit",
            expected_line,
        )
    ] * 2


def test_duplicate_json_keys_are_rejected_before_schema_dispatch():
    source = (
        '{"schema_version":"0.0.1",'
        '"schema_version":"0.0.1","signatures":[]}'
    )

    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "duplicate JSON key 'schema_version'" in result.file_errors[0].message
    # stdlib object_pairs_hook exposes duplicate order but not source offsets;
    # do not publish a misleading line number.
    assert result.file_errors[0].line is None


def test_duplicate_yaml_keys_report_second_key_line():
    pytest.importorskip("yaml")
    source = (
        "schema_version: '0.0.1'\n"
        "schema_version: '0.0.1'\n"
        "signatures: []\n"
    )

    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "found duplicate key 'schema_version'" in result.file_errors[0].message
    assert result.file_errors[0].line == 2


@pytest.mark.parametrize(
    "source",
    (
        '{"schema_version":"0.0.1","value":NaN,"signatures":[]}',
        "schema_version: '0.0.1'\nvalue: .inf\nsignatures: []\n",
    ),
)
def test_nonfinite_numbers_are_rejected_during_parsing(source):
    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "non-finite" in result.file_errors[0].message


def test_source_size_limit_is_applied_before_parsing():
    result = load_rules(" " * (MAX_SOURCE_BYTES + 1))

    assert result.file_errors[0].code == "parse_error"
    assert "exceeds {} bytes".format(MAX_SOURCE_BYTES) in (
        result.file_errors[0].message
    )


@pytest.mark.parametrize(
    "source",
    (
        b" " * (MAX_SOURCE_BYTES + 1),
        BytesIO(b" " * (MAX_SOURCE_BYTES + 1)),
    ),
)
def test_byte_source_size_is_checked_before_decode(source):
    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "exceeds {} bytes".format(MAX_SOURCE_BYTES) in (
        result.file_errors[0].message
    )


def test_document_depth_limit_is_applied_before_schema_validation():
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


@pytest.mark.parametrize(
    "extension, expected",
    (
        ("x" * (MAX_SCALAR_BYTES + 1), "scalar larger"),
        (list(range(MAX_COLLECTION_ITEMS + 1)), "collection exceeds"),
        (
            [[None] * 9 for unused in range(MAX_COLLECTION_ITEMS)],
            "exceeds {} nodes".format(MAX_DOCUMENT_NODES),
        ),
    ),
)
def test_direct_document_validation_enforces_resource_limits(
    extension, expected
):
    document = _document()
    document["vendor_extension"] = extension

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert result.file_errors[0].code == "parse_error"
    assert expected in result.file_errors[0].message


def test_direct_document_validation_rejects_cycles():
    document = _document()
    cycle = []
    cycle.append(cycle)
    document["vendor_extension"] = cycle

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert result.file_errors[0].code == "parse_error"
    assert "recursive aliases" in result.file_errors[0].message


def test_yaml_aliases_are_rejected_before_construction():
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


def test_signature_count_limit_is_file_scoped():
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
    assert result.file_errors[0].code == "too_many_signatures"
    assert result.file_errors[0].path == "$.signatures"


def test_event_count_limit_is_rule_scoped_in_mixed_generation():
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
        issue.code == "schema_maxItems"
        and issue.path
        == "$.signatures[1].signature.conditions.events"
        for issue in result.broken_rules[0].issues
    )


def test_real_schema_rejection_falls_back_to_active_generation(tmp_path):
    paths = _lifecycle_paths(tmp_path)
    os.makedirs(os.path.dirname(paths.inbox), exist_ok=True)
    os.makedirs(paths.rules_dir, exist_ok=True)
    active_source = json.dumps(_document(), sort_keys=True)
    Path(paths.active).write_text(active_source)

    invalid = _document()
    del invalid["signatures"][0]["signature"]["metadata"]["severity"]
    Path(paths.inbox).write_text(json.dumps(invalid))
    _accept_inbox(paths)

    result = RuleGenerationManager(
        paths, _candidate_validation, "platform-v1"
    ).activate()

    assert result.source == "active"
    assert result.fallback_used is True
    assert Path(paths.active).read_text() == active_source
    manifest = json.loads(Path(paths.manifest).read_text())
    rejected = next(
        attempt
        for attempt in manifest["activation_attempts"]
        if attempt["source"] == "inbox"
    )
    assert rejected["validation_result"] == "FAILED"
    assert rejected["usable_rule_count"] == 0
    assert "zero usable rules" in rejected["reason"]


def test_mixed_schema_candidate_activates_degraded(tmp_path):
    paths = _lifecycle_paths(tmp_path)
    os.makedirs(os.path.dirname(paths.inbox), exist_ok=True)
    candidate = _document()
    bad = _unique_rule_copy(candidate)
    _file_unit_schema_violation(bad)
    candidate["signatures"].append(bad)
    source = json.dumps(candidate, sort_keys=True)
    Path(paths.inbox).write_text(source)
    _accept_inbox(paths)

    result = RuleGenerationManager(
        paths, _candidate_validation, "platform-v1"
    ).activate()

    assert result.source == "inbox"
    assert result.validation_result == "DEGRADED"
    assert result.fallback_used is False
    assert len(result.broken_rules) == 1
    assert Path(paths.active).read_text() == source
