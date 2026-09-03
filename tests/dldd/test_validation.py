from __future__ import absolute_import

from copy import deepcopy
import json
import logging
from pathlib import Path

import pytest

from dldd.adapters import RedisAdapter
from dldd.dse import (
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
from dldd.models import ResolvedSource, ValueConfig
from dldd.planner import build_plans
from dldd.validation import (
    ValidationContext,
    validate_document,
)
from tests.dldd_fakes import (
    append_rule,
    valid_rule_action,
    valid_rule_event as event,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name="valid-redis-rule.json"):
    with (FIXTURES / name).open() as stream:
        return json.load(stream)


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
            lambda unused_invocation: ResolvedEvaluation(
                expected_value=0,
                operator=">=",
            ),
        )


class DirectSensorDSEHook(SensorDSEHook):
    def resolve_source(self, reference, context):
        return (
            ResolvedSource(
                type="redis",
                path={
                    "database": "STATE_DB",
                    "table": "TEMPERATURE_INFO",
                    "key": "TEMPERATURE_INFO|TEMP0",
                    "path": "temperature",
                },
                instance="TEMP0",
                value_configs=ValueConfig(type="float", unit="C"),
            ),
        )


class RecordingDirectSensorDSEHook(DirectSensorDSEHook):
    def __init__(self):
        self.comparator_instances = []

    def resolve_evaluation(self, reference, context):
        def get_comparator(invocation):
            self.comparator_instances.append(invocation.binding.instance)
            return ResolvedEvaluation(
                expected_value=10.0,
                operator=">=",
                value_configs=ValueConfig(type="float", unit="C"),
            )

        return DSEEvaluationHandle(reference, get_comparator)


class FixedMetadataDSEHook(SensorDSEHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(
            expected_value=10.0,
            operator=">=",
            value_configs=ValueConfig(type="float", unit="vendor-units"),
        )


def test_activation_materialization_prefers_nondefault_rule_value_config():
    document = load_fixture()
    configured = event(document)
    configured["evaluation"] = {
        "type": "dse",
        "operator": ">=",
        "value": "sensor:get_high_threshold()",
        "value_configs": {"type": "N/A", "unit": "rule-units"},
    }

    result = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=FixedMetadataDSEHook())),
    )

    assert result.activation_valid
    config = result.materialized_rules[0].events[0].event.evaluation.value_configs
    assert config == ValueConfig(type="N/A", unit="rule-units")


def test_static_source_planning_prefers_nondefault_rule_value_config():
    document = load_fixture()
    configured = event(document)
    configured.update(
        {
            "type": "dse",
            "path": "sensor:get_value()",
        }
    )
    configured["evaluation"]["value_configs"] = {
        "type": "N/A",
        "unit": "rule-units",
    }
    result = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=DirectSensorDSEHook())),
    )
    assert result.activation_valid

    bundle = build_plans(
        result.materialized_rules,
        "sha256:value-config-precedence",
        {"redis": 1, "file": 1, "common": 1},
    )

    item = next(iter(bundle.work_items.values()))
    assert item.value_config == ValueConfig(type="N/A", unit="rule-units")


@pytest.mark.parametrize(
    "source_reference, evaluation_reference, hook_factory, plan_collection, warning",
    (
        pytest.param(
            "sensor:get_value()",
            "{sensor*}:{get_high_threshold()}",
            SensorDSEHook,
            "templates",
            "cardinality",
            id="fixed-source-instanced-evaluation",
        ),
        pytest.param(
            "{sensor*}:{get_value()}",
            "{voltage*}:{get_high_threshold()}",
            SensorDSEHook,
            "templates",
            "selector",
            id="runtime-cross-selector",
        ),
        pytest.param(
            "{sensor*}:{get_value()}",
            "{sensor*}:{get_high_threshold()}",
            SensorDSEHook,
            None,
            None,
            id="runtime-matching-selector",
        ),
    ),
)
def test_selector_mapping_warns_once_only_for_unusual_executable_mappings(
    source_reference,
    evaluation_reference,
    hook_factory,
    plan_collection,
    warning,
    caplog,
):
    document = load_fixture()
    configured = event(document)
    configured["type"] = "dse"
    configured["path"] = source_reference
    configured["evaluation"] = {
        "type": "dse",
        "value": evaluation_reference,
    }

    with caplog.at_level(logging.WARNING, logger="dldd.validation"):
        result = validate_document(
            document,
            ValidationContext(dse_registry=DSERegistry(hook=hook_factory())),
        )

    assert result.file_valid
    assert len(result.materialized_rules) == 1
    assert not result.broken_rules
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 1, "file": 1, "common": 1},
    )
    if plan_collection is not None:
        assert getattr(bundle, plan_collection)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "cross-selector mapping" in record.getMessage()
    ]
    if warning is None:
        assert warnings == []
    else:
        assert len(warnings) == 1
        source_selector = source_reference.split(":", 1)[0].strip("{}")
        evaluation_selector = evaluation_reference.split(":", 1)[0].strip("{}")
        assert source_selector in warnings[0]
        assert evaluation_selector in warnings[0]
        if warning == "selector":
            assert "explicitly maps DSE source selector" in warnings[0]


def test_matching_direct_dse_source_executes_runtime_comparator_for_instance():
    document = load_fixture()
    configured = event(document)
    configured["type"] = "dse"
    configured["path"] = "{sensor*}:{get_value()}"
    configured["evaluation"] = {
        "type": "dse",
        "value": "{sensor*}:{get_high_threshold()}",
    }
    hook = RecordingDirectSensorDSEHook()

    result = validate_document(
        document,
        ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 1, "file": 1, "common": 1},
    )
    item = next(iter(bundle.work_items.values()))
    adapter = RedisAdapter(
        lambda unused_database, unused_table, unused_key: {
            "temperature": "12.5"
        }
    )

    evaluated = adapter.collect(item)

    assert evaluated.result.value == "MATCH"
    assert evaluated.value.normalized == 12.5
    assert hook.comparator_instances == ["TEMP0"]


def test_direct_materialization_applies_defaults_and_preserves_schema_provenance():
    result = validate_document(load_fixture())

    assert result.file_valid
    assert result.activation_valid
    assert not result.broken_rules
    rule = result.materialized_rules[0]
    assert result.ruleset.schema_version == result.schema_version
    assert result.ruleset.signatures[0].schema_version == result.schema_version
    assert rule.signature.schema_version == result.schema_version
    assert rule.metadata.priority == 5
    assert rule.events[0].sources[0].type == "redis"
    local_action = rule.signature.actions.repair_actions.local_actions.action_list[0]
    assert local_action.timeout == 300

    plans = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 60, "file": 60, "common": 60},
    )

    assert {
        item.schema_version for item in plans.work_items.values()
    } == {result.schema_version}


@pytest.mark.parametrize(
    "field, values, expected_code",
    (
        pytest.param(None, None, None, id="matching-positionals"),
        pytest.param(
            "key",
            ["TEMPERATURE_INFO|TEMP0"],
            "instance_path_mismatch",
            id="key-count-mismatch",
        ),
        pytest.param(
            "evaluation",
            [75.0],
            "instance_value_mismatch",
            id="value-count-mismatch",
        ),
    ),
)
def test_redis_positional_materialization_requires_matching_instance_counts(
    field, values, expected_code
):
    document = load_fixture()
    configured = event(document)
    configured["instances"] = [
        "TEMP0:TEMPERATURE_INFO|TEMP0",
        "TEMP1:TEMPERATURE_INFO|TEMP1",
    ]
    if field is None:
        configured["path"]["key"] = [
            "TEMPERATURE_INFO|TEMP0",
            "TEMPERATURE_INFO|TEMP1",
        ]
        configured["evaluation"]["value"] = [75.0, 85.0]
    elif field == "evaluation":
        configured["evaluation"]["value"] = values
    else:
        configured["path"][field] = values

    result = validate_document(document)
    if expected_code is not None:
        assert expected_code in {
            issue.code for issue in result.broken_rules[0].issues
        }
    else:
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
        items = sorted(
            plans.work_items.values(), key=lambda item: item.component_name
        )
        assert [item.event_id for item in items] == [1, 1]
        assert [item.evaluation["value"] for item in items] == [75.0, 85.0]


@pytest.mark.parametrize(
    "mutate, expected_code, expected_path",
    (
        pytest.param(None, None, None, id="vendor-identities"),
        pytest.param(
            lambda document: document["signatures"][0]["signature"][
                "metadata"
            ].pop("component"),
            "missing_field",
            "$.signatures[0].signature.metadata.component",
            id="missing-component",
        ),
        pytest.param(
            lambda document: document["signatures"][0]["signature"][
                "actions"
            ]["repair_actions"]["remote_actions"].update(
                {"action_list": [7]}
            ),
            "invalid_type",
            "$.signatures[0].signature.actions.repair_actions."
            "remote_actions.action_list[0]",
            id="non-string-remote-action",
        ),
    ),
)
def test_extensible_identity_fields_accept_vendor_values_but_remain_strict(
    mutate, expected_code, expected_path
):
    document = load_fixture()
    if mutate is None:
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
    else:
        mutate(document)
        result = validate_document(document, materialize=False)
        assert not result.activation_valid
        assert any(
            issue.code == expected_code and issue.path == expected_path
            for issue in result.broken_rules[0].issues
        )


@pytest.mark.parametrize(
    "value, expected_code",
    (
        pytest.param("omitted", None, id="omitted"),
        pytest.param(86400, None, id="explicit-day"),
        pytest.param(None, "invalid_type", id="null"),
        pytest.param(2**32, "out_of_range", id="overflow"),
    ),
)
def test_event_sampling_interval_is_optional_strict_and_materialized(
    value, expected_code
):
    document = load_fixture()
    if value != "omitted":
        event(document)["sampling_interval"] = value

    result = validate_document(document)
    if expected_code is None:
        expected = None if value == "omitted" else value
        assert result.activation_valid
        validated_event = result.ruleset.signatures[0].conditions.events[0]
        assert validated_event.sampling_interval == expected
        assert (
            result.materialized_rules[0].events[0].event.sampling_interval
            == expected
        )
    else:
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


@pytest.mark.parametrize(
    "value, valid",
    (
        pytest.param("omitted", True, id="omitted"),
        pytest.param(True, True, id="enabled"),
        pytest.param("true", False, id="string"),
    ),
)
def test_event_async_collection_is_optional_strict_and_materialized(value, valid):
    document = load_fixture()
    if value != "omitted":
        event(document)["async"] = value

    result = validate_document(document)
    if valid:
        expected = value is True
        assert result.activation_valid
        assert (
            result.ruleset.signatures[0].conditions.events[0].async_collection
            is expected
        )
        assert (
            result.materialized_rules[0].events[0].event.async_collection
            is expected
        )
    else:
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
    "operation_type, expected_field",
    (
        pytest.param("cli", "argv", id="malformed-cli"),
        pytest.param([], None, id="list-type"),
    ),
)
def test_operation_discriminator_localizes_malformed_builtin_and_unhashable_types(
    operation_type, expected_field
):
    document = load_fixture()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    if expected_field is None:
        action["type"] = operation_type
    else:
        action.clear()
        action.update(
            {"type": operation_type, "timeout": 10, "vendor_only": "unsafe"}
        )

    result = validate_document(document)
    base = (
        "$.signatures[0].signature.actions.repair_actions.local_actions."
        "action_list[0].action"
    )
    issues = [(issue.code, issue.path) for issue in result.broken_rules[0].issues]
    if expected_field is None:
        assert not result.file_valid
        assert issues == [("invalid_type", base + ".type")]
    else:
        assert ("missing_field", "{}.{}".format(base, expected_field)) in issues
        assert ("unknown_field", "{}.vendor_only".format(base)) in issues


def test_platform_vendor_positional_lists_must_match_instances():
    document = load_fixture()
    configured = event(document)
    configured["type"] = "platform_api"
    configured["instances"] = ["FAN0:first", "FAN1:second"]
    configured["path"] = {"hook": "read_fault", "channels": ["first"]}

    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert any(
        issue.code == "instance_path_mismatch"
        for issue in result.broken_rules[0].issues
    )


class RecordingVendorSourceHook(DSEHook):
    """Record side-effect-free validation of direct vendor source mappings."""

    def __init__(self):
        self.validated_sources = []

    def resolve_source(self, reference, context):
        raise AssertionError("direct source unexpectedly used DSE resolution")

    def resolve_evaluation(self, reference, context):
        raise AssertionError("direct source unexpectedly resolved an evaluator")

    def validate_resolved_source(self, source, context):
        self.validated_sources.append((source, context))


def test_direct_vendor_source_requires_and_uses_an_advertised_typed_hook():
    document = load_fixture()
    event(document).update(type="platform_api", path={"hook": "read_fault"})

    missing = validate_document(
        document,
        ValidationContext(
            dse_registry=DSERegistry(source_types=("platform_api",))
        ),
    )
    assert not missing.activation_valid
    assert "requires an installed DSE hook" in (
        missing.broken_rules[0].issues[0].message
    )

    hook = RecordingVendorSourceHook()
    supported = validate_document(
        document,
        ValidationContext(
            dse_registry=DSERegistry(
                hook=hook,
                source_types=("platform_api",),
            )
        ),
    )

    assert supported.activation_valid
    assert len(hook.validated_sources) == 1
    source, context = hook.validated_sources[0]
    assert source.type == "platform_api"
    assert context.rule_name == "PSU_OV_FAULT"


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

    assert not result.file_valid
    assert any(
        issue.code == "invalid_type" and issue.path.endswith(".action.path.value")
        for issue in result.broken_rules[0].issues
    )


@pytest.mark.parametrize("scenario", ("duplicate", "mixed", "zero-usable"))
def test_validation_localizes_rule_failures_and_enforces_activation_guard(scenario):
    document = load_fixture()
    if scenario == "duplicate":
        document["signatures"].append(deepcopy(document["signatures"][0]))
    elif scenario == "mixed":
        append_rule(document, name="BAD_SOURCE", rule_id=1000002)
        event(document, rule_index=1).update(type="not-installed", path={})
    else:
        event(document).update(type="not-installed", path={})

    result = validate_document(document)
    if scenario == "duplicate":
        assert not result.file_valid
        assert {issue.code for issue in result.file_errors} == {
            "duplicate_rule_id",
            "duplicate_rule_name",
        }
    else:
        assert not result.file_valid
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


def _duplicate_first_event(document):
    conditions = document["signatures"][0]["signature"]["conditions"]
    conditions["events"].append(deepcopy(conditions["events"][0]))


def _set_i2c_action_without_value(document):
    action = valid_rule_action(document)
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
            },
        }
    )


@pytest.mark.parametrize(
    "mutation, expected_code",
    (
        pytest.param(
            lambda doc: event(doc).update({"match_count": 0}),
            "out_of_range",
            id="zero-match-count",
        ),
        pytest.param(
            lambda doc: doc["signatures"][0]["signature"]["conditions"].update(
                {"logic": "1 AND 2"}
            ),
            "invalid_logic",
            id="unknown-logic-event",
        ),
        pytest.param(
            lambda doc: event(doc).update(
                {"evaluation": {"type": "string", "operator": "regex", "value": "["}}
            ),
            "invalid_regex",
            id="invalid-regex-expression",
        ),
        pytest.param(
            lambda doc: event(doc).update(
                match_count=2,
                match_period=0,
            ),
            "invalid_match_window",
            id="invalid-current-state-match-window",
        ),
        pytest.param(
            _duplicate_first_event,
            "duplicate_event_id",
            id="duplicate-event-id",
        ),
        pytest.param(
            _set_i2c_action_without_value,
            "missing_i2c_value",
            id="i2c-set-without-value",
        ),
        pytest.param(
            lambda doc: doc["signatures"][0]["signature"]["actions"].update(
                log_collection={}
            ),
            "empty_log_collection",
            id="empty-log-collection",
        ),
    ),
)
def test_semantic_validation(mutation, expected_code):
    document = load_fixture()
    mutation(document)

    result = validate_document(document)

    assert expected_code in {issue.code for issue in result.broken_rules[0].issues}


def test_mask_evaluation_rejects_values_that_cannot_execute():
    document = load_fixture()
    event(document)["evaluation"] = {
        "type": "mask",
        "logic": "&",
        "value": "not-an-integer",
    }

    result = validate_document(document)

    value_path = (
        "$.signatures[0].signature.conditions.events[0].event.evaluation.value"
    )
    assert ("invalid_format", value_path) in {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }


def test_direct_i2c_positional_and_cli_materialization_contracts():
    """Validate read-only I2C positional paths and immutable CLI argv."""

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

    source_event["path"]["bus"] = ["IO-MUX-6"]
    result = validate_document(document)
    assert "instance_path_mismatch" in {
        issue.code for issue in result.broken_rules[0].issues
    }

    source_event["path"].update(bus=["IO-MUX-6", "IO-MUX-7"], i2c_type="set")
    result = validate_document(document)
    assert "unsupported_value" in {
        issue.code for issue in result.broken_rules[0].issues
    }

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


@pytest.mark.parametrize(
    "kind, hook",
    (
        pytest.param("multiple", MultiOperationHook(), id="multiple-operations"),
        pytest.param("cli", CLIHook(), id="immutable-cli-argv"),
    ),
)
def test_dse_resolved_source_families_survive_preflight_and_planning(
    kind, hook
):
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})
    result = validate_document(
        document,
        context=ValidationContext(dse_registry=DSERegistry(hook=hook)),
    )

    assert result.activation_valid
    if kind == "cli":
        assert result.materialized_rules[0].events[0].sources[0].path[
            "argv"
        ] == ("/usr/bin/printf", "51")
        return

    bundle = build_plans(
        result.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    assert len(bundle.work_items) == 2
    assert len({item.source_id for item in bundle.work_items.values()}) == 2
    assert {item.source["key"] for item in bundle.work_items.values()} == {
        "PSU_INFO|PSU0|A",
        "PSU_INFO|PSU0|B",
    }


@pytest.mark.parametrize(
    "kind, reference",
    (
        pytest.param("brace", "{PSU}:get_fault()", id="braced-selector-only"),
        pytest.param(
            "wildcard", "{psu?}:{get_fault()}", id="wildcard-without-instance"
        ),
        pytest.param("source", "PSU:get_fault(1)", id="source-with-argument"),
    ),
)
def test_dse_reference_contract_localizes_malformed_and_unbound_patterns(
    kind, reference
):
    if kind == "brace":
        with pytest.raises(DSEReferenceError, match="matching braces"):
            parse_reference(reference)
        return

    document = load_fixture()
    event(document).update({"type": "dse", "path": reference})
    expected_path = "$.signatures[0].signature.conditions.events[0].event.path"
    context = (
        ValidationContext(dse_registry=DSERegistry(hook=CLIHook()))
        if kind == "wildcard"
        else None
    )
    result = validate_document(
        document,
        context=context,
        materialize=kind == "wildcard",
    )

    assert not result.activation_valid
    if kind == "wildcard":
        assert "must identify each component instance" in (
            result.broken_rules[0].issues[0].message
        )
        assert parse_reference(reference).canonical == reference
    else:
        assert ("invalid_format", expected_path) in {
            (issue.code, issue.path) for issue in result.broken_rules[0].issues
        }


class InvalidTypedSourceHook(FakeHook):
    def resolve_source(self, reference, context):
        return (ResolvedSource(type="redis", path={}, instance=1),)


def test_dse_hook_results_require_typed_identity():
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


def test_dse_document_materialization_and_source_resolution_failures():
    """Materialize complete DSE rules and localize only expected resolution gaps."""

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

    # Missing integration is localized; an actual vendor bug still propagates.
    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})

    result = validate_document(document)

    assert not result.activation_valid
    assert result.broken_rules[0].issues[0].code == "dse_hook_unresolved"

    class BuggyHook(FakeHook):
        def resolve_source(self, reference, context):
            raise RuntimeError("vendor implementation bug")

    document = load_fixture()
    event(document).update({"type": "dse", "path": "PSU:get_fault()"})
    context = ValidationContext(dse_registry=DSERegistry(hook=BuggyHook()))

    with pytest.raises(RuntimeError, match="vendor implementation bug"):
        validate_document(document, context=context)


@pytest.mark.parametrize(
    "mode",
    (
        pytest.param("missing-semantics", id="missing-semantics"),
        pytest.param("comparator", id="hook-comparator"),
        pytest.param("rule-operator", id="operator-needs-value"),
    ),
)
def test_dse_evaluation_requires_complete_comparator_or_operator_contract(mode):
    document = load_fixture()
    evaluation = {
        "type": "dse",
        "value": "{psu*}:{failure_value()}",
    }
    if mode == "rule-operator":
        evaluation["operator"] = "equals"
        hook = ComparatorWithoutExpectedValueHook()
    elif mode == "comparator":
        hook = ComparatorHook()
    else:
        hook = FakeHook()
    event(document).update(
        {
            "type": "dse",
            "path": "{psu*}:{get_fault()}",
            "evaluation": evaluation,
        }
    )
    context = ValidationContext(dse_registry=DSERegistry(hook=hook))

    result = validate_document(document, context=context)
    if mode == "comparator":
        assert result.activation_valid
        comparator = (
            result.materialized_rules[0].events[0].event.evaluation.comparator
        )
        assert comparator("fault") is True
        assert comparator("healthy") is False
    else:
        assert not result.activation_valid
        expected = (
            "requires a resolved expected value"
            if mode == "rule-operator"
            else "comparator semantics"
        )
        assert expected in result.broken_rules[0].issues[0].message


class ComparatorHook(FakeHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(comparator=lambda value: value == "fault")


@pytest.mark.parametrize(
    "context, expected",
    (
        pytest.param(
            ValidationContext(
                product_id="OTHER-PRODUCT",
                software_version="202311.3.0.1",
            ),
            "does not apply to product",
            id="product-mismatch",
        ),
        pytest.param(
            ValidationContext(
                software_version="202311.3.0.1",
                require_compatibility_identity=True,
            ),
            "product identity is unavailable",
            id="missing-product",
        ),
    ),
)
def test_platform_compatibility_and_identity_fail_only_the_affected_rule(
    context, expected
):
    result = validate_document(load_fixture(), context=context)

    assert result.file_valid
    assert not result.activation_valid
    assert expected in result.broken_rules[0].issues[0].message


class ComparatorWithoutExpectedValueHook(FakeHook):
    def resolve_evaluation(self, reference, context):
        return ResolvedEvaluation(comparator=lambda value: bool(value))


class VendorHook(FakeHook):
    def validate_vendor_operation(self, operation, context):
        if "token" not in operation.options:
            raise ValueError("vendor action requires token")


@pytest.mark.parametrize("mode", ("vendor-action", "i2c-query"))
def test_vendor_operations_require_explicit_advertisement_and_hook_validation(mode):
    document = load_fixture()
    if mode == "vendor-action":
        local_actions = document["signatures"][0]["signature"]["actions"][
            "repair_actions"
        ]["local_actions"]
        action = local_actions["action_list"][0]["action"]
        action.clear()
        action.update(
            {"type": "vendor_reset", "token": "safe", "timeout": 10}
        )
        context = ValidationContext(
            dse_registry=DSERegistry(
                hook=VendorHook(), action_types=("vendor_reset",)
            )
        )
    else:
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
        context = ValidationContext(
            dse_registry=DSERegistry(hook=FakeHook(), query_types=("i2c",))
        )

    unsupported = validate_document(document)
    assert "dse_hook_unresolved" in {
        issue.code for issue in unsupported.broken_rules[0].issues
    }
    assert "not advertised" in unsupported.broken_rules[0].issues[0].message

    supported = validate_document(document, context=context)
    assert supported.activation_valid
    if mode == "i2c-query":
        query = supported.ruleset.signatures[0].actions.log_collection.queries[0]
        assert query.path["command"] == "0x7A"
