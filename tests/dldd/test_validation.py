from __future__ import absolute_import

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd.dse import (
    DSEContext,
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSEHook,
    DSEReferenceError,
    DSERegistry,
    DSESourceHandle,
    DSEUnresolvedError,
    ResolvedCommand,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.models import ResolvedSource, ValueConfig, to_mutable
from dldd.platform import PlatformIdentity, load_extensions
from dldd.planner import build_plans
from dldd.rule_schema import DEFAULT_CONTRACT_REGISTRY
from dldd.rule_schema.generate import (
    GENERATED_WARNING,
    JSON_SCHEMA_DIALECT,
    default_output_path,
    generate_schema,
    render_schema,
)
from dldd.validation import (
    CompatibilityMatcher,
    ValidationContext,
    load_rules,
    validate_document,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name="valid-redis-rule.json"):
    with (FIXTURES / name).open() as stream:
        return json.load(stream)


def event(document, signature=0, index=0):
    return document["signatures"][signature]["signature"]["conditions"]["events"][index]["event"]


class SensorDSEHook(DSEHook):
    def resolve_source(self, reference, context):
        return DSESourceHandle(
            reference,
            lambda unused_context: DSEExpansionResult(()),
            lambda unused_invocation: None,
        )

    def resolve_evaluation(self, reference, context):
        return DSEEvaluationHandle(
            reference,
            lambda unused_invocation: {
                "type": "dse",
                "operator": ">=",
                "value": 0,
            },
        )


def test_exact_pydantic_contract_is_the_runtime_authority():
    assert DEFAULT_CONTRACT_REGISTRY.versions == ("0.0.1",)
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact("0.0.1")

    envelope = contract.validate_envelope(load_fixture())

    assert envelope.schema_version == "0.0.1"
    assert len(envelope.signatures) == 1


@pytest.mark.parametrize(
    "source_reference,evaluation_reference",
    (
        (
            "sensor:redis_sensor_value()",
            "{sensor*}:{redis_high_threshold()}",
        ),
        (
            "{sensor*}:{redis_sensor_value()}",
            "{other*}:{redis_high_threshold()}",
        ),
    ),
)
def test_instanced_dse_evaluator_requires_matching_instanced_source(
    source_reference, evaluation_reference
):
    document = load_fixture()
    configured = event(document)
    configured["type"] = "dse"
    configured["path"] = source_reference
    configured["evaluation"] = {
        "type": "dse",
        "value": evaluation_reference,
    }

    result = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=SensorDSEHook())),
    )

    assert result.file_valid
    assert not result.materialized_rules
    issue = result.broken_rules[0].issues[0]
    assert issue.code == "materialization_failed"
    assert "instanced DSE" in issue.message


def test_generated_json_schema_is_a_current_derivative():
    schema = generate_schema("0.0.1")

    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["properties"]["schema_version"]["const"] == "0.0.1"
    assert schema["x-dldd-schema-version"] == "0.0.1"
    assert schema["x-generated-warning"] == GENERATED_WARNING
    assert "does not load this file at runtime" in GENERATED_WARNING
    assert default_output_path("0.0.1").read_text() == render_schema("0.0.1")


def test_valid_direct_rule_materializes_and_applies_defaults():
    result = validate_document(load_fixture())

    assert result.file_valid
    assert result.activation_valid
    assert not result.broken_rules
    rule = result.materialized_rules[0]
    assert rule.metadata.priority == 5
    assert rule.events[0].sources[0].type == "redis"
    local_action = rule.signature.actions.repair_actions.local_actions.action_list[0]
    assert local_action.timeout == 300


def test_one_redis_event_expands_positionally_across_sensor_instances():
    document = load_fixture()
    configured = event(document)
    configured["instances"] = [
        "TEMP0:TEMPERATURE_INFO|TEMP0",
        "TEMP1:TEMPERATURE_INFO|TEMP1",
    ]
    configured["path"]["key"] = [
        "TEMPERATURE_INFO|TEMP0",
        "TEMPERATURE_INFO|TEMP1",
    ]
    configured["evaluation"]["value"] = [75.0, 85.0]

    result = validate_document(document)

    assert result.activation_valid
    rule = result.materialized_rules[0]
    assert rule.signature.conditions.logic == "1"
    assert len(rule.events) == 1
    assert [source.path["key"] for source in rule.events[0].sources] == [
        "TEMPERATURE_INFO|TEMP0",
        "TEMPERATURE_INFO|TEMP1",
    ]

    plans = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    items = sorted(plans.work_items.values(), key=lambda item: item.component_name)
    assert [item.event_id for item in items] == [1, 1]
    assert [item.evaluation["value"] for item in items] == [75.0, 85.0]


@pytest.mark.parametrize(
    "field, values, expected_code",
    (
        ("key", ["TEMPERATURE_INFO|TEMP0"], "instance_path_mismatch"),
        ("evaluation", [75.0], "instance_value_mismatch"),
    ),
)
def test_redis_positional_values_must_match_instances(
    field, values, expected_code
):
    document = load_fixture()
    configured = event(document)
    configured["instances"] = [
        "TEMP0:TEMPERATURE_INFO|TEMP0",
        "TEMP1:TEMPERATURE_INFO|TEMP1",
    ]
    if field == "evaluation":
        configured["evaluation"]["value"] = values
    else:
        configured["path"][field] = values

    result = validate_document(document)

    assert expected_code in {
        issue.code for issue in result.broken_rules[0].issues
    }


def test_mixed_sensor_rules_keep_three_usable_and_isolate_two_broken():
    result = load_rules(
        str(FIXTURES / "mixed-sensor-rules.yaml"),
        ValidationContext(
            product_id="8102_28fh_dpu_o",
            software_version="grboudre_dldd-impl.0-1d85491a7",
            require_compatibility_identity=True,
            dse_registry=DSERegistry(hook=SensorDSEHook()),
        ),
    )

    assert result.file_valid
    all_rules = {
        rule.metadata.id: rule for rule in result.materialized_rules
    }
    rules = {
        rule_id: rule
        for rule_id, rule in all_rules.items()
        if rule_id in (9999101, 9999102, 9999103)
    }
    assert set(all_rules) == {
        9999101,
        9999102,
        9999103,
        9999401,
        9999402,
        9999403,
    }
    assert set(rules) == {9999101, 9999102, 9999103}
    assert {rule_id: rule.metadata.name for rule_id, rule in rules.items()} == {
        9999101: "DLDD_TEMPERATURE_HIGH",
        9999102: "DLDD_VOLTAGE_HIGH",
        9999103: "DLDD_CURRENT_HIGH",
    }
    expected_counts = {9999101: 71, 9999102: 218, 9999103: 28}
    expected_tables = {
        9999101: ("TEMPERATURE_INFO", "temperature"),
        9999102: ("VOLTAGE_INFO", "voltage"),
        9999103: ("CURRENT_INFO", "current"),
    }
    thresholds = {}
    for rule_id, rule in rules.items():
        table, reading = expected_tables[rule_id]
        assert rule.signature.conditions.logic == "1"
        assert len(rule.events) == 1
        materialized_event = rule.events[0]
        assert len(materialized_event.sources) == expected_counts[rule_id]
        assert all(
            source.path["table"] == table
            and source.path["key"].startswith(table + "|")
            and source.path["path"] == reading
            for source in materialized_event.sources
        )
        keys = [source.path["key"] for source in materialized_event.sources]
        assert len(keys) == len(set(keys))
        values = materialized_event.event.evaluation.value
        assert len(values) == expected_counts[rule_id]
        thresholds.update(dict(zip(keys, values)))
        assert {source.instance for source in materialized_event.sources} == {
            binding.split(":", 1)[0]
            for binding in materialized_event.event.instances
        }
    assert thresholds["TEMPERATURE_INFO|X86_PKG_TEMP"] == 115.0
    assert thresholds["VOLTAGE_INFO|P12V_CPU"] == 13200.0
    assert thresholds["CURRENT_INFO|P12V_SLED1_IIN"] == 17141.0

    plans = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )
    assert len(plans.work_items) == 317
    assert len(plans.templates) == 3
    assert {
        rule_id: sum(
            item.rule_id == rule_id for item in plans.work_items.values()
        )
        for rule_id in rules
    } == expected_counts
    assert {
        item.event_id
        for item in plans.work_items.values()
        if item.rule_id in rules
    } == {1}
    broken = {rule.rule_id: rule for rule in result.broken_rules}
    assert set(broken) == {9999201, 9999202}
    assert {issue.code for issue in broken[9999201].issues} == {
        "missing_field"
    }
    assert {issue.code for issue in broken[9999202].issues} == {
        "unsupported_value"
    }


def test_component_and_remote_action_identities_are_extensible():
    document = load_fixture()
    metadata = document["signatures"][0]["signature"]["metadata"]
    metadata["component"] = "VENDOR_FABRIC_MODULE"
    remote = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["remote_actions"]
    remote["action_list"] = [
        "ACTION_RESEAT",
        "vendor-healthz:ACTION_REPAIR_FABRIC_MODULE",
    ]

    result = validate_document(document)

    assert result.activation_valid
    signature = result.ruleset.signatures[0]
    assert signature.metadata.component == "VENDOR_FABRIC_MODULE"
    assert signature.actions.repair_actions.remote_actions.action_list == (
        "ACTION_RESEAT",
        "vendor-healthz:ACTION_REPAIR_FABRIC_MODULE",
    )


@pytest.mark.parametrize(
    "mutate, expected_code, expected_path",
    (
        (
            lambda document: document["signatures"][0]["signature"][
                "metadata"
            ].pop("component"),
            "missing_field",
            "$.signatures[0].signature.metadata.component",
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "metadata"
            ].update({"component": ""}),
            "invalid_length",
            "$.signatures[0].signature.metadata.component",
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "metadata"
            ].update({"component": 7}),
            "invalid_type",
            "$.signatures[0].signature.metadata.component",
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "actions"
            ]["repair_actions"]["remote_actions"].update(
                {"action_list": [""]}
            ),
            "invalid_length",
            "$.signatures[0].signature.actions.repair_actions."
            "remote_actions.action_list[0]",
        ),
        (
            lambda document: document["signatures"][0]["signature"][
                "actions"
            ]["repair_actions"]["remote_actions"].update(
                {"action_list": [7]}
            ),
            "invalid_type",
            "$.signatures[0].signature.actions.repair_actions."
            "remote_actions.action_list[0]",
        ),
    ),
)
def test_extensible_identity_fields_remain_required_nonempty_strict_strings(
    mutate, expected_code, expected_path
):
    document = load_fixture()
    mutate(document)

    result = validate_document(document, materialize=False)

    assert not result.activation_valid
    assert any(
        issue.code == expected_code and issue.path == expected_path
        for issue in result.broken_rules[0].issues
    )


def test_event_sampling_interval_is_optional_and_strictly_materialized():
    document = load_fixture()

    omitted = validate_document(document)
    assert omitted.activation_valid
    omitted_event = omitted.ruleset.signatures[0].conditions.events[0]
    assert omitted_event.sampling_interval is None
    assert (
        omitted.materialized_rules[0].events[0].event.sampling_interval is None
    )

    event(document)["sampling_interval"] = 86400
    explicit = validate_document(document)
    assert explicit.activation_valid
    explicit_event = explicit.ruleset.signatures[0].conditions.events[0]
    assert explicit_event.sampling_interval == 86400
    assert (
        explicit.materialized_rules[0].events[0].event.sampling_interval
        == 86400
    )


def test_event_async_collection_is_optional_strict_and_materialized():
    document = load_fixture()

    omitted = validate_document(document)
    assert omitted.activation_valid
    assert not omitted.ruleset.signatures[0].conditions.events[0].async_collection
    assert not omitted.materialized_rules[0].events[0].event.async_collection

    event(document)["async"] = True
    enabled = validate_document(document)
    assert enabled.activation_valid
    assert enabled.ruleset.signatures[0].conditions.events[0].async_collection
    assert enabled.materialized_rules[0].events[0].event.async_collection


@pytest.mark.parametrize("value", (None, 0, 1, "true", 0.0))
def test_event_async_collection_rejects_non_boolean_values(value):
    document = load_fixture()
    event(document)["async"] = value

    result = validate_document(document)

    assert not result.activation_valid
    assert {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    } == {
        (
            "invalid_type",
            "$.signatures[0].signature.conditions.events[0].event.async",
        )
    }


@pytest.mark.parametrize(
    "value, expected_code",
    (
        (None, "invalid_type"),
        (True, "invalid_type"),
        ("60", "invalid_type"),
        (60.0, "invalid_type"),
        (0, "out_of_range"),
        (2**32, "out_of_range"),
    ),
)
def test_event_sampling_interval_rejects_null_coercion_and_bad_ranges(
    value, expected_code
):
    document = load_fixture()
    event(document)["sampling_interval"] = value

    result = validate_document(document)

    assert not result.activation_valid
    assert {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    } == {
        (
            expected_code,
            "$.signatures[0].signature.conditions.events[0].event."
            "sampling_interval",
        )
    }


def test_omitted_optional_timeout_is_distinct_from_explicit_null():
    document = load_fixture()
    del document["local_action_default_timeout"]
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action["timeout"] = 30

    omitted = validate_document(document)
    assert omitted.activation_valid
    assert omitted.ruleset.local_action_default_timeout is None

    document["local_action_default_timeout"] = None
    explicit_null = validate_document(document)
    assert not explicit_null.file_valid
    assert [
        (issue.code, issue.path) for issue in explicit_null.file_errors
    ] == [("invalid_type", "$.local_action_default_timeout")]


def test_contract_fields_are_closed_but_vendor_payloads_are_preserved():
    document = load_fixture()
    document["unexpected_root_field"] = True

    file_failure = validate_document(document)
    assert [(issue.code, issue.path) for issue in file_failure.file_errors] == [
        ("unknown_field", "$.unexpected_root_field")
    ]

    del document["unexpected_root_field"]
    event(document)["unexpected_event_field"] = True
    rule_failure = validate_document(document)
    assert [
        (issue.code, issue.path) for issue in rule_failure.broken_rules[0].issues
    ] == [
        (
            "unknown_field",
            "$.signatures[0].signature.conditions.events[0].event."
            "unexpected_event_field",
        )
    ]

    del event(document)["unexpected_event_field"]
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update(
        {
            "type": "vendor_reset",
            "timeout": 10,
            "token": "safe",
            "policy": {
                "attempts": 2,
                "flags": [True, None, "cold"],
            },
        }
    )

    vendor = validate_document(document, materialize=False)
    assert vendor.file_valid
    assert not vendor.broken_rules
    operation = vendor.ruleset.signatures[0].actions.repair_actions
    operation = operation.local_actions.action_list[0]
    assert operation.options["token"] == "safe"
    assert operation.options["policy"]["attempts"] == 2
    assert operation.options["policy"]["flags"] == (True, None, "cold")


@pytest.mark.parametrize(
    "mutation, expected_path",
    (
        (
            lambda doc: doc["signatures"][0]["signature"]["metadata"].update(
                {"id": "1000001"}
            ),
            "$.signatures[0].signature.metadata.id",
        ),
        (
            lambda doc: event(doc).update({"match_count": True}),
            "$.signatures[0].signature.conditions.events[0].event.match_count",
        ),
        (
            lambda doc: event(doc).update(
                {
                    "evaluation": {
                        "type": "string",
                        "operator": "equals",
                        "value": "fault",
                        "case_sensitive": 1,
                    }
                }
            ),
            "$.signatures[0].signature.conditions.events[0].event.evaluation."
            "case_sensitive",
        ),
    ),
)
def test_core_scalar_fields_do_not_coerce(mutation, expected_path):
    document = load_fixture()
    mutation(document)

    result = validate_document(document)

    assert any(
        issue.code == "invalid_type" and issue.path == expected_path
        for issue in result.broken_rules[0].issues
    )


@pytest.mark.parametrize(
    "operation_type, expected_field",
    (("cli", "argv"), ("dse", "command"), ("i2c", "path")),
)
def test_malformed_builtin_operation_never_falls_through_vendor_model(
    operation_type, expected_field
):
    document = load_fixture()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update(
        {"type": operation_type, "timeout": 10, "vendor_only": "unsafe"}
    )

    result = validate_document(document)

    issues = {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }
    base = (
        "$.signatures[0].signature.actions.repair_actions.local_actions."
        "action_list[0].action"
    )
    assert ("missing_field", "{}.{}".format(base, expected_field)) in issues
    assert ("unknown_field", "{}.vendor_only".format(base)) in issues


@pytest.mark.parametrize("invalid_type", ([], {}))
def test_unhashable_operation_type_is_a_rule_diagnostic(invalid_type):
    document = load_fixture()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action["type"] = invalid_type

    result = validate_document(document)

    assert result.file_valid
    assert [(issue.code, issue.path) for issue in result.broken_rules[0].issues] == [
        (
            "invalid_type",
            "$.signatures[0].signature.actions.repair_actions.local_actions."
            "action_list[0].action.type",
        )
    ]


def test_platform_vendor_positional_lists_must_match_instances():
    document = load_fixture()
    configured = event(document)
    configured["type"] = "platform_api"
    configured["instances"] = ["FAN0:first", "FAN1:second"]
    configured["path"] = {"hook": "read_fault", "channels": ["first"]}

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert any(
        issue.code == "instance_path_mismatch"
        for issue in result.broken_rules[0].issues
    )


def test_i2c_set_action_rejects_explicit_null_value():
    document = load_fixture()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update(
        {
            "type": "i2c",
            "timeout": 10,
            "path": {
                "bus": "IO-MUX-6",
                "chip_addr": "0x58",
                "i2c_type": "set",
                "command": "0x7A",
                "size": "b",
                "value": None,
            },
        }
    )

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert any(
        issue.code == "invalid_type" and issue.path.endswith(".action.path.value")
        for issue in result.broken_rules[0].issues
    )


def test_json_source_is_safely_loaded():
    result = load_rules(json.dumps(load_fixture()))

    assert result.activation_valid


def test_yaml_source_uses_safe_loader():
    yaml = pytest.importorskip("yaml")
    result = load_rules(yaml.safe_dump(load_fixture()))

    assert result.activation_valid
    assert load_rules("!!python/object/apply:os.system ['echo unsafe']").file_errors


def test_parse_and_file_gate_errors_reject_candidate():
    assert load_rules("{not json").file_errors[0].code == "parse_error"
    assert validate_document([]).file_errors[0].code == "invalid_top_level"
    assert validate_document({"schema_version": "9.0.0", "signatures": [{}]}).file_errors


def test_duplicate_rule_identity_is_a_file_level_error():
    document = load_fixture()
    document["signatures"].append(deepcopy(document["signatures"][0]))

    result = validate_document(document)

    assert not result.file_valid
    assert {issue.code for issue in result.file_errors} == {
        "duplicate_rule_id",
        "duplicate_rule_name",
    }


def test_bad_rule_is_isolated_when_another_rule_is_usable():
    document = load_fixture()
    bad = load_fixture("invalid-unknown-event.json")["signatures"][0]
    document["signatures"].append(bad)

    result = validate_document(document)

    assert result.activation_valid
    assert len(result.usable_rules) == 1
    assert result.broken_rules[0].rule_name == "BAD_SOURCE"
    assert "unsupported_type" in {
        issue.code for issue in result.broken_rules[0].issues
    }


def test_zero_usable_rules_fails_activation_guard():
    result = validate_document(load_fixture("invalid-unknown-event.json"))

    assert result.file_valid
    assert not result.activation_valid
    assert len(result.broken_rules) == 1


def test_local_action_requires_timeout_or_file_default():
    document = load_fixture()
    del document["local_action_default_timeout"]

    result = validate_document(document)

    assert not result.activation_valid
    assert "missing_action_timeout" in {
        issue.code for issue in result.broken_rules[0].issues
    }


@pytest.mark.parametrize(
    "mutation, expected_code",
    (
        (lambda doc: event(doc).update({"match_count": 0}), "out_of_range"),
        (lambda doc: event(doc).update({"match_period": 3601}), "out_of_range"),
        (
            lambda doc: doc["signatures"][0]["signature"]["conditions"].update(
                {"logic": "1 AND 2"}
            ),
            "invalid_logic",
        ),
        (
            lambda doc: event(doc).update(
                {"evaluation": {"type": "string", "operator": "regex", "value": "["}}
            ),
            "invalid_regex",
        ),
    ),
)
def test_semantic_validation(mutation, expected_code):
    document = load_fixture()
    mutation(document)

    result = validate_document(document)

    assert expected_code in {issue.code for issue in result.broken_rules[0].issues}


@pytest.mark.parametrize("value", ("", "not-an-integer", "0b102", "0xGG"))
def test_mask_evaluation_rejects_values_that_cannot_execute(value):
    document = load_fixture()
    event(document)["evaluation"] = {
        "type": "mask",
        "logic": "&",
        "value": value,
    }

    result = validate_document(document)

    value_path = (
        "$.signatures[0].signature.conditions.events[0].event.evaluation.value"
    )
    assert ("invalid_format", value_path) in {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }


def test_direct_i2c_monitoring_is_read_only_and_instance_lists_are_positional():
    document = load_fixture()
    source_event = event(document)
    source_event.update(
        {
            "type": "i2c",
            "instances": ["PSU0:IO-MUX-6", "PSU1:IO-MUX-7"],
            "path": {
                "bus": ["IO-MUX-6", "IO-MUX-7"],
                "chip_addr": "0x58",
                "i2c_type": "get",
                "command": "0x7A",
                "size": "b",
            },
            "evaluation": {"type": "mask", "logic": "&", "value": "0b10000000"},
        }
    )

    result = validate_document(document)

    assert result.activation_valid
    sources = result.materialized_rules[0].events[0].sources
    assert [(source.instance, source.path["bus"]) for source in sources] == [
        ("PSU0", "IO-MUX-6"),
        ("PSU1", "IO-MUX-7"),
    ]
    assert sources[0].vendor_data["path_identifier"] == "IO-MUX-6"

    source_event["path"]["i2c_type"] = "set"
    result = validate_document(document)
    assert (
        "unsupported_value",
        "$.signatures[0].signature.conditions.events[0].event.path.i2c_type",
    ) in {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }


def test_instance_and_positional_path_lengths_must_match():
    document = load_fixture()
    source_event = event(document)
    source_event.update(
        {
            "type": "i2c",
            "instances": ["PSU0:IO-MUX-6", "PSU1:IO-MUX-7"],
            "path": {
                "bus": ["IO-MUX-6"],
                "chip_addr": "0x58",
                "i2c_type": "get",
                "command": "0x7A",
                "size": "b",
            },
            "evaluation": {"type": "mask", "logic": "&", "value": "0b1"},
        }
    )

    result = validate_document(document)

    assert "instance_path_mismatch" in {
        issue.code for issue in result.broken_rules[0].issues
    }


def test_list_valued_i2c_bus_requires_positional_instances():
    document = load_fixture()
    event(document).update(
        {
            "type": "i2c",
            "path": {
                "bus": ["IO-MUX-6", "IO-MUX-7"],
                "chip_addr": "0x58",
                "i2c_type": "get",
                "command": "0x7A",
                "size": "b",
            },
            "evaluation": {"type": "mask", "logic": "&", "value": "0b1"},
        }
    )

    result = validate_document(document)

    assert "instance_path_mismatch" in {
        issue.code for issue in result.broken_rules[0].issues
    }


def test_direct_cli_argv_survives_immutable_materialization():
    document = load_fixture()
    event(document).update(
        {
            "type": "cli",
            "path": {"argv": ["/usr/bin/printf", "51"], "timeout": 5},
        }
    )

    result = validate_document(document)

    assert result.activation_valid
    assert result.materialized_rules[0].events[0].sources[0].path["argv"] == (
        "/usr/bin/printf",
        "51",
    )


class FakeHook(DSEHook):
    def resolve_source(self, reference, context):
        if reference.function != "get_fault":
            raise DSEUnresolvedError("source is not exposed")
        return tuple(
            ResolvedSource(
                type="redis",
                path={
                    "database": "STATE_DB",
                    "table": "PSU_INFO",
                    "key": "PSU_INFO|{}".format(instance),
                    "path": "fault",
                },
                instance=instance,
            )
            for instance in ("PSU0", "PSU1")
        )

    def resolve_evaluation(self, reference, context):
        if reference.function != "failure_value":
            raise DSEUnresolvedError("evaluation is not exposed")
        return ResolvedEvaluation(expected_value=True)

    def resolve_action(self, reference, context):
        return ResolvedCommand(executor=lambda operation: None)

    def resolve_query(self, reference, context):
        return ResolvedCommand(executor=lambda operation: None)


class CLIHook(FakeHook):
    def resolve_source(self, reference, context):
        return (
            ResolvedSource(
                type="cli",
                path={"argv": ["/usr/bin/printf", "51"], "timeout": 5},
            ),
        )


class MultiOperationHook(FakeHook):
    def resolve_source(self, reference, context):
        return tuple(
            ResolvedSource(
                type="redis",
                path={
                    "database": "STATE_DB",
                    "table": "PSU_INFO",
                    "key": "PSU_INFO|PSU0|{}".format(suffix),
                    "path": "fault",
                },
                instance="PSU0",
                vendor_data={"operation": suffix},
            )
            for suffix in ("A", "B")
        )


def test_distinct_dse_operations_for_one_instance_survive_planning():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})
    result = validate_document(
        document,
        context=ValidationContext(
            dse_registry=DSERegistry(hook=MultiOperationHook())
        ),
    )

    bundle = build_plans(
        result.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )

    assert len(bundle.work_items) == 2
    assert len({item.source_id for item in bundle.work_items.values()}) == 2
    assert {
        item.source["key"] for item in bundle.work_items.values()
    } == {"PSU_INFO|PSU0|A", "PSU_INFO|PSU0|B"}


def test_dse_resolved_cli_argv_survives_immutable_materialization():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})

    result = validate_document(
        document,
        context=ValidationContext(dse_registry=DSERegistry(hook=CLIHook())),
    )

    assert result.activation_valid
    assert result.materialized_rules[0].events[0].sources[0].path["argv"] == (
        "/usr/bin/printf",
        "51",
    )


@pytest.mark.parametrize(
    "reference",
    ("{PSU}:get_fault()", "PSU:{get_fault()}"),
)
def test_dse_reference_rejects_mixed_brace_forms(reference):
    with pytest.raises(DSEReferenceError, match="matching braces"):
        parse_reference(reference)


def test_question_mark_wildcard_dse_requires_instance_identity():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "{psu?}:{get_fault()}"})

    result = validate_document(
        document,
        context=ValidationContext(dse_registry=DSERegistry(hook=CLIHook())),
    )

    assert not result.activation_valid
    assert "must identify each component instance" in (
        result.broken_rules[0].issues[0].message
    )
    assert parse_reference("{psu?}:{get_fault()}").canonical == (
        "{psu?}:{get_fault()}"
    )


@pytest.mark.parametrize(
    "field, value",
    (
        ("path", "PSU:get_fault(1)"),
        ("path", "{psu*}:get_fault()"),
    ),
)
def test_pydantic_contract_rejects_malformed_dse_source_references(field, value):
    document = load_fixture()
    event(document).update({"type": "dse", field: value})

    result = validate_document(document, materialize=False)

    assert (
        "invalid_format",
        "$.signatures[0].signature.conditions.events[0].event.path",
    ) in {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }


def test_pydantic_contract_rejects_malformed_dse_evaluation_reference():
    document = load_fixture()
    event(document)["evaluation"] = {
        "type": "dse",
        "operator": "equals",
        "value": "PSU:failure_value",
    }
    result = validate_document(document, materialize=False)

    assert {
        issue.path for issue in result.broken_rules[0].issues
        if issue.code == "invalid_format"
    } == {
        "$.signatures[0].signature.conditions.events[0].event.evaluation.value",
    }


class InvalidTypedSourceHook(FakeHook):
    def resolve_source(self, reference, context):
        return (ResolvedSource(type="redis", path={}, instance=1),)


def test_dse_hook_must_return_typed_source_identity():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})

    result = validate_document(
        document,
        context=ValidationContext(
            dse_registry=DSERegistry(hook=InvalidTypedSourceHook())
        ),
    )

    assert not result.activation_valid
    assert "instance must be a non-empty string" in (
        result.broken_rules[0].issues[0].message
    )


class InvalidValueConfigHook(FakeHook):
    def resolve_source(self, reference, context):
        return (
            ResolvedSource(
                type="redis",
                path={
                    "database": "STATE_DB",
                    "table": "PSU_INFO",
                    "key": "PSU_INFO|PSU0",
                    "path": "fault",
                },
                value_configs=ValueConfig(type="pickle", unit="N/A"),
            ),
        )


def test_dse_hook_value_config_uses_canonical_contract():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})

    result = validate_document(
        document,
        context=ValidationContext(
            dse_registry=DSERegistry(hook=InvalidValueConfigHook())
        ),
    )

    assert not result.activation_valid
    assert "type must use a canonical value" in (
        result.broken_rules[0].issues[0].message
    )


class InvalidEvaluationValueConfigHook(FakeHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(
            expected_value=True,
            value_configs=ValueConfig(type="pickle", unit="N/A"),
        )


def test_dse_evaluation_value_config_uses_canonical_contract():
    document = load_fixture()
    event(document).update(
        {
            "type": "dse",
            "path": "PSU:get_fault()",
            "evaluation": {
                "type": "dse",
                "operator": "equals",
                "value": "PSU:failure_value()",
            },
        }
    )

    result = validate_document(
        document,
        context=ValidationContext(
            dse_registry=DSERegistry(
                hook=InvalidEvaluationValueConfigHook()
            )
        ),
    )

    assert not result.activation_valid
    assert "evaluation value_configs" in (
        result.broken_rules[0].issues[0].message
    )
    assert "type must use a canonical value" in (
        result.broken_rules[0].issues[0].message
    )


def test_dse_source_evaluation_action_and_query_use_explicit_hook():
    document = load_fixture()
    source_event = event(document)
    source_event.update(
        {
            "type": "dse",
            "path": "{psu*}:{get_fault()}",
            "evaluation": {
                "type": "dse",
                "operator": "equals",
                "value": "{psu*}:{failure_value()}",
            },
        }
    )
    actions = document["signatures"][0]["signature"]["actions"]
    actions["repair_actions"]["local_actions"]["action_list"] = [
        {"action": {"type": "dse", "command": "PSU:reset()", "timeout": 20}}
    ]
    actions["log_collection"]["queries"] = [
        {"query": {"type": "dse", "command": "PSU:get_status()"}}
    ]
    context = ValidationContext(dse_registry=DSERegistry(hook=FakeHook()))

    result = validate_document(document, context=context)

    assert result.activation_valid
    materialized_event = result.materialized_rules[0].events[0]
    assert materialized_event.sources[0].instance == "PSU0"
    assert materialized_event.event.evaluation.value is True
    materialized = result.materialized_rules[0].signature.actions
    assert callable(materialized.repair_actions.local_actions.action_list[0].executor)
    assert callable(materialized.log_collection.queries[0].executor)


def test_complete_hld_example_is_a_positive_yaml_fixture():
    pytest.importorskip("yaml")
    context = ValidationContext(dse_registry=DSERegistry(hook=FakeHook()))

    result = load_rules(FIXTURES / "valid-psu-hld.yaml", context=context)

    assert result.activation_valid
    assert len(result.materialized_rules[0].events) == 2
    assert {
        source.instance
        for source in result.materialized_rules[0].events[1].sources
    } == {"PSU0", "PSU1"}


def test_dse_without_hook_is_a_rule_materialization_failure():
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})

    result = validate_document(document)

    assert not result.activation_valid
    assert result.broken_rules[0].issues[0].code == "materialization_failed"


def test_unexpected_vendor_materializer_error_is_not_blame_on_rule():
    class BuggyHook(FakeHook):
        def resolve_source(self, reference, context):
            raise RuntimeError("vendor implementation bug")

    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})
    context = ValidationContext(dse_registry=DSERegistry(hook=BuggyHook()))

    with pytest.raises(RuntimeError, match="vendor implementation bug"):
        validate_document(document, context=context)


def test_dse_evaluation_without_operator_requires_hook_comparator():
    document = load_fixture()
    event(document).update(
        {
            "type": "dse",
            "path": "{psu*}:{get_fault()}",
            "evaluation": {
                "type": "dse",
                "value": "{psu*}:{failure_value()}",
            },
        }
    )
    context = ValidationContext(dse_registry=DSERegistry(hook=FakeHook()))

    result = validate_document(document, context=context)

    assert not result.activation_valid
    assert "comparator semantics" in result.broken_rules[0].issues[0].message


class ComparatorHook(FakeHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(comparator=lambda value: value == "fault")


def test_dse_hook_can_supply_complete_comparator_contract():
    document = load_fixture()
    event(document).update(
        {
            "type": "dse",
            "path": "{psu*}:{get_fault()}",
            "evaluation": {
                "type": "dse",
                "value": "{psu*}:{failure_value()}",
            },
        }
    )
    context = ValidationContext(dse_registry=DSERegistry(hook=ComparatorHook()))

    result = validate_document(document, context=context)

    assert result.activation_valid
    comparator = result.materialized_rules[0].events[0].event.evaluation.comparator
    assert comparator("fault") is True
    assert comparator("healthy") is False


def test_platform_compatibility_is_a_rule_level_gate():
    document = load_fixture()
    context = ValidationContext(
        product_id="OTHER-PRODUCT", software_version="202311.3.0.1"
    )

    result = validate_document(document, context=context)

    assert result.file_valid
    assert not result.activation_valid
    assert "does not apply to product" in result.broken_rules[0].issues[0].message


class PrefixCompatibilityMatcher(CompatibilityMatcher):
    def product_matches(self, current_product, supported_products):
        return any(current_product.startswith(item) for item in supported_products)

    def software_matches(self, current_version, supported_versions):
        return any(current_version.startswith(item) for item in supported_versions)


def test_platform_can_supply_compatibility_matching_contract(monkeypatch):
    matcher = PrefixCompatibilityMatcher()
    module = SimpleNamespace(
        create_compatibility_matcher=lambda **kwargs: matcher
    )
    monkeypatch.setattr(
        "dldd.platform.importlib.import_module", lambda name: module
    )

    extensions = load_extensions(
        PlatformIdentity("test", "PRODUCT-REV2", "202311.3-build"),
        "/missing/dse.yaml",
    )

    assert extensions.compatibility_matcher is matcher


def test_platform_carries_optional_artifact_client_factory(monkeypatch):
    def create_artifact_client(**kwargs):
        return kwargs

    module = SimpleNamespace(create_artifact_client=create_artifact_client)
    monkeypatch.setattr(
        "dldd.platform.importlib.import_module", lambda name: module
    )

    extensions = load_extensions(
        PlatformIdentity("test", "PRODUCT", "SOFTWARE"),
        "/missing/dse.yaml",
    )

    assert extensions.artifact_client_factory is create_artifact_client


def test_platform_rejects_noncallable_artifact_client_factory(monkeypatch):
    module = SimpleNamespace(create_artifact_client="not-callable")
    monkeypatch.setattr(
        "dldd.platform.importlib.import_module", lambda name: module
    )

    with pytest.raises(TypeError, match="create_artifact_client must be callable"):
        load_extensions(
            PlatformIdentity("test", "PRODUCT", "SOFTWARE"),
            "/missing/dse.yaml",
        )


def test_activation_requires_detected_platform_identity():
    result = validate_document(
        load_fixture(),
        context=ValidationContext(require_compatibility_identity=True),
    )

    assert not result.activation_valid
    assert "product identity is unavailable" in result.broken_rules[0].issues[0].message


class ComparatorWithoutExpectedValueHook(FakeHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(comparator=lambda value: bool(value))


def test_dse_rule_operator_requires_a_resolved_expected_value():
    document = load_fixture()
    event(document).update(
        {
            "type": "dse",
            "path": "{psu*}:{get_fault()}",
            "evaluation": {
                "type": "dse",
                "operator": "equals",
                "value": "{psu*}:{failure_value()}",
            },
        }
    )
    context = ValidationContext(
        dse_registry=DSERegistry(hook=ComparatorWithoutExpectedValueHook())
    )

    result = validate_document(document, context=context)

    assert not result.activation_valid
    assert "requires a resolved expected value" in result.broken_rules[0].issues[0].message


class VendorHook(FakeHook):
    def validate_vendor_operation(self, operation, context):
        if "token" not in operation.options:
            raise ValueError("vendor action requires token")


def test_vendor_action_types_must_be_advertised_and_hook_validated():
    document = load_fixture()
    local_actions = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]
    action = local_actions["action_list"][0]["action"]
    action.clear()
    action.update({"type": "vendor_reset", "token": "safe", "timeout": 10})

    unsupported = validate_document(document)
    assert "materialization_failed" in {
        issue.code for issue in unsupported.broken_rules[0].issues
    }
    assert "not advertised" in unsupported.broken_rules[0].issues[0].message

    context = ValidationContext(
        dse_registry=DSERegistry(hook=VendorHook(), action_types=("vendor_reset",))
    )
    supported = validate_document(document, context=context)
    assert supported.activation_valid


def test_i2c_log_query_requires_explicit_platform_support():
    document = load_fixture()
    queries = document["signatures"][0]["signature"]["actions"][
        "log_collection"
    ]["queries"]
    queries[0]["query"] = {
        "type": "i2c",
        "path": {
            "bus": "IO-MUX-6",
            "chip_addr": "0x58",
            "i2c_type": "get",
            "command": "0x7A",
            "size": "b",
        },
    }

    unsupported = validate_document(document)
    assert "materialization_failed" in {
        issue.code for issue in unsupported.broken_rules[0].issues
    }
    assert "not advertised" in unsupported.broken_rules[0].issues[0].message

    context = ValidationContext(
        dse_registry=DSERegistry(hook=FakeHook(), query_types=("i2c",))
    )
    supported = validate_document(document, context=context)
    assert supported.activation_valid
    query = supported.ruleset.signatures[0].actions.log_collection.queries[0]
    assert query.path["command"] == "0x7A"


def test_rule_models_are_frozen():
    result = validate_document(load_fixture())
    metadata = result.ruleset.signatures[0].metadata

    with pytest.raises(FrozenInstanceError):
        metadata.priority = 9

    source = result.materialized_rules[0].events[0].sources[0]
    with pytest.raises(TypeError):
        source.path["database"] = "CONFIG_DB"
    assert to_mutable(source)["path"]["database"] == "STATE_DB"
