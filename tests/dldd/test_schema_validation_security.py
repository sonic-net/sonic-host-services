from __future__ import absolute_import

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from io import BytesIO
import json
import os
from pathlib import Path
import pkgutil

import pytest
from pydantic import TypeAdapter, ValidationError

from dldd.lifecycle import (
    CandidateValidation,
    RuleGenerationManager,
    RulePaths,
    sha256_file,
)
from dldd.logic import MAX_LOGIC_NESTING
from dldd.rule_schema import (
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    ContractRegistryError,
    normalize_validation_error,
)
from dldd.rule_schema.generate import (
    GENERATED_WARNING,
    JSON_SCHEMA_DIALECT,
    generate_schema,
    render_schema,
)
from dldd.rule_schema.v0_0_1 import MAX_REGEX_NESTING
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


FIXTURES = Path(__file__).parent / "fixtures"


def _document():
    return json.loads((FIXTURES / "valid-redis-rule.json").read_text())


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


def test_packaged_derivative_schema_is_current_for_installed_contract():
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


def test_generated_schema_is_documentation_not_a_runtime_dependency(
    monkeypatch,
):
    document = _document()

    def unexpected_schema_read(*unused_args, **unused_kwargs):
        pytest.fail("runtime validation read a generated schema artifact")

    monkeypatch.setattr(Path, "read_text", unexpected_schema_read)

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert len(result.ruleset.signatures) == 1


@pytest.mark.parametrize("version", ("0.0.2", "0.0", "latest", "0.0.1 "))
def test_registry_dispatches_only_exact_installed_versions(version):
    with pytest.raises(ContractRegistryError, match="unsupported schema_version"):
        DEFAULT_CONTRACT_REGISTRY.require_exact(version)

    document = _document()
    document["schema_version"] = version
    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert [(issue.code, issue.path) for issue in result.file_errors] == [
        ("unsupported_schema_version", "$.schema_version")
    ]


def test_registry_rejects_empty_and_inconsistent_code_contracts():
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


def test_envelope_rejects_unknown_core_fields_at_file_scope():
    document = _document()
    document["future_option"] = True

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert [(issue.code, issue.path) for issue in result.file_errors] == [
        ("unknown_field", "$.future_option")
    ]


def test_missing_required_field_is_rule_scoped_in_mixed_generation():
    document = _document()
    bad = _unique_rule_copy(document)
    del bad["signature"]["metadata"]["severity"]
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert len(result.broken_rules) == 1
    contract_issue = next(
        issue
        for issue in result.broken_rules[0].issues
        if issue.code == "missing_field"
    )
    assert contract_issue.path == (
        "$.signatures[1].signature.metadata.severity"
    )


def test_yaml_specific_scalar_inside_signature_remains_rule_scoped():
    yaml = pytest.importorskip("yaml")
    document = _document()
    bad = _unique_rule_copy(document, "YAML_DATE_BAD", 1000002)
    bad["signature"]["metadata"]["description"] = date(2026, 7, 6)
    document["signatures"].append(bad)

    result = load_rules(yaml.safe_dump(document, sort_keys=False))

    assert result.file_valid
    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert [(issue.code, issue.path) for issue in result.broken_rules[0].issues] == [
        (
            "invalid_type",
            "$.signatures[1].signature.metadata.description",
        )
    ]


def test_unknown_signature_core_field_is_rule_scoped():
    document = _document()
    bad = _unique_rule_copy(document)
    bad["signature"]["metadata"]["future_option"] = "not-installed"
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert len(result.usable_rules) == 1
    assert [(issue.code, issue.path) for issue in result.broken_rules[0].issues] == [
        (
            "unknown_field",
            "$.signatures[1].signature.metadata.future_option",
        )
    ]


@pytest.mark.parametrize(
    "mutate, expected_path",
    (
        (
            lambda wrapper: wrapper["signature"]["metadata"].update(
                {"id": "1000001"}
            ),
            "$.signatures[1].signature.metadata.id",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"id": 1.0}),
            "$.signatures[1].signature.conditions.events[0].event.id",
        ),
        (
            lambda wrapper: wrapper["signature"]["conditions"]["events"][0][
                "event"
            ].update({"match_count": True}),
            "$.signatures[1].signature.conditions.events[0].event.match_count",
        ),
    ),
)
def test_strict_contract_does_not_coerce_rule_values(mutate, expected_path):
    document = _document()
    bad = _unique_rule_copy(document)
    mutate(bad)
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.file_valid
    assert len(result.usable_rules) == 1
    assert [(issue.code, issue.path) for issue in result.broken_rules[0].issues] == [
        ("invalid_type", expected_path)
    ]


def test_huge_yaml_integer_rule_id_is_localized_without_formatting_it():
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


def test_runtime_serialized_integer_fields_have_explicit_safe_bounds():
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


def test_present_constraint_is_enforced_per_rule():
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
        if issue.code == "invalid_length"
    ] == [
        (
            "invalid_length",
            "$.signatures[1].signature.conditions.events[0].event.path.unit",
        )
    ]


def test_pydantic_constraint_has_one_stable_operator_diagnostic():
    document = _document()
    event = document["signatures"][0]["signature"]["conditions"]["events"][
        0
    ]["event"]
    event["match_period"] = 3601

    result = validate_document(document)

    assert [issue.code for issue in result.broken_rules[0].issues] == [
        "out_of_range"
    ]


def test_contract_defaults_do_not_mutate_input():
    document = _document()
    metadata = document["signatures"][0]["signature"]["metadata"]
    metadata.pop("priority", None)
    original = deepcopy(document)

    result = validate_document(document, materialize=False)

    assert document == original
    assert result.ruleset.signatures[0].metadata.priority == 5


def test_omitted_event_sampling_interval_is_distinct_from_explicit_null():
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


def test_omitted_envelope_timeout_is_distinct_from_explicit_null():
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


@pytest.mark.parametrize(
    "mutate, expected_path, file_scoped",
    (
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
    ),
)
def test_second_fields_are_bounded_unsigned_32_bit(
    mutate, expected_path, file_scoped
):
    document = _document()
    mutate(document)

    result = validate_document(document)
    issues = result.file_errors if file_scoped else result.broken_rules[0].issues

    assert any(
        issue.code == "out_of_range" and issue.path == expected_path
        for issue in issues
    )


def test_generated_schema_publishes_optional_but_non_null_fields():
    schema = generate_schema("0.0.1")
    redis_event = schema["$defs"]["RedisEventV001"]

    assert "sampling_interval" not in redis_event["required"]
    assert redis_event["properties"]["sampling_interval"]["type"] == "integer"
    assert "local_action_default_timeout" not in schema["required"]
    assert schema["properties"]["local_action_default_timeout"][
        "type"
    ] == "integer"


def test_vendor_operation_has_bounded_extension_boundary():
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


def test_hostile_vendor_payload_is_rejected_without_value_disclosure():
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


@pytest.mark.parametrize(
    "vendor_key", ("vendor", "cli", "dict[str,...]", "FooV001")
)
def test_vendor_keys_that_resemble_union_internals_keep_canonical_path(
    vendor_key,
):
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
            "$.signatures[0].signature.actions.repair_actions.local_actions."
            "action_list[0].action{}".format(suffix),
        )
    ]


@pytest.mark.parametrize(
    "mapping_key, path_suffix",
    (
        (1, "[1]"),
        (1.5, '["1.5"]'),
        (
            date(2026, 7, 6),
            '["datetime.date(2026, 7, 6)"]',
        ),
    ),
)
def test_non_string_vendor_mapping_key_omits_pydantic_key_marker(
    mapping_key, path_suffix
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


def test_literal_vendor_key_marker_uses_escaped_property_path():
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


def test_bounded_paths_keep_distinct_hash_suffixes():
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


def test_error_normalization_is_deterministic_redacted_and_bounded():
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


def test_complete_candidate_diagnostics_have_issue_and_byte_caps():
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


def test_control_character_identities_cannot_expand_diagnostic_budget():
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


def test_contract_issue_path_and_line_are_stable_across_runs():
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


def test_duplicate_json_keys_are_rejected_before_contract_dispatch():
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
        "schema_version: '0.0.1'\n.nan: value\nsignatures: []\n",
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


def test_mapping_keys_are_subject_to_scalar_size_limit():
    document = _document()
    document["k" * (MAX_SCALAR_BYTES + 1)] = True

    result = validate_document(document, materialize=False)

    assert result.file_errors[0].code == "parse_error"
    assert "scalar larger than {} bytes".format(MAX_SCALAR_BYTES) in (
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


def test_document_depth_limit_is_applied_before_contract_validation():
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
    assert result.file_errors[0].code == "invalid_length"
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
        issue.code == "invalid_length"
        and issue.path
        == "$.signatures[1].signature.conditions.events"
        for issue in result.broken_rules[0].issues
    )


def test_real_contract_rejection_falls_back_to_active_generation(tmp_path):
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


def test_mixed_contract_candidate_activates_degraded(tmp_path):
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
