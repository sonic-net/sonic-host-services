from __future__ import absolute_import

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd.dse import (
    DSEContext,
    DSEHook,
    DSEReferenceError,
    DSERegistry,
    DSEUnresolvedError,
    ResolvedCommand,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.models import ResolvedSource, ValueConfig, to_mutable
from dldd.platform import PlatformIdentity, load_extensions
from dldd.planner import build_plans
from dldd.validation import (
    CompatibilityMatcher,
    SCHEMA_PATH,
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


def test_machine_readable_schema_is_valid_json():
    with open(SCHEMA_PATH) as stream:
        schema = json.load(stream)

    assert schema["$schema"].endswith("draft-07/schema#")
    assert schema["properties"]["schema_version"]["const"] == "0.0.1"


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
    assert "unsupported_event_type" in {
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

    assert "invalid_mask_value" in {
        issue.code for issue in result.broken_rules[0].issues
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
    assert "invalid_i2c_operation" in {
        issue.code for issue in result.broken_rules[0].issues
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
def test_static_schema_rejects_malformed_dse_source_references(field, value):
    document = load_fixture()
    event(document).update({"type": "dse", field: value})

    result = validate_document(document, materialize=False)

    assert "invalid_dse_reference" in {
        issue.code for issue in result.broken_rules[0].issues
    }


def test_static_schema_rejects_malformed_dse_evaluation_reference():
    document = load_fixture()
    event(document)["evaluation"] = {
        "type": "dse",
        "operator": "equals",
        "value": "PSU:failure_value",
    }
    result = validate_document(document, materialize=False)

    assert {
        issue.path for issue in result.broken_rules[0].issues
        if issue.code == "invalid_dse_reference"
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
    assert "unsupported_operation_type" in {
        issue.code for issue in unsupported.broken_rules[0].issues
    }

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
    assert "unsupported_operation_type" in {
        issue.code for issue in unsupported.broken_rules[0].issues
    }

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
