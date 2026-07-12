from __future__ import absolute_import

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dldd.rule_schema import DEFAULT_CONTRACT_REGISTRY
from dldd.rule_schema.v0_0_1 import (
    I2CActionPathV001,
    MAX_REGEX_CHARACTERS,
    VendorOperationV001,
    _evaluation_to_domain,
    _event_path_to_domain,
    _operation_discriminator,
    _operation_to_domain,
    _vendor_operation_json_schema,
)
from dldd.validation import validate_document
from tests.dldd_fakes import (
    load_valid_rules_document as _document,
    valid_rule_event as _event,
)


def _domain(document):
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact(document["schema_version"])
    wrapper = contract.validate_signature(document["signatures"][0])
    return contract.to_domain(
        wrapper,
        local_action_default_timeout=document.get("local_action_default_timeout"),
    )


def _issue_codes(document):
    result = validate_document(document, materialize=False)
    assert result.file_valid
    assert len(result.broken_rules) == 1
    return {issue.code for issue in result.broken_rules[0].issues}


def test_versioned_evaluation_and_event_semantic_boundaries():
    """Compile supported evaluations and localize event-level semantic errors."""

    for evaluation, expected in (
        (
            {
                "type": "string",
                "operator": "regex",
                "value": r"\([()]+\)",
            },
            ("regex", r"\([()]+\)", True),
        ),
        (
            {
                "type": "string",
                "operator": "equals",
                "value": "FAULT",
                "case_sensitive": False,
            },
            ("equals", "FAULT", False),
        ),
        (
            {
                "type": "string",
                "operator": "regex",
                "value": "a" * (MAX_REGEX_CHARACTERS + 1),
            },
            "invalid_regex",
        ),
    ):
        document = _document()
        _event(document)["evaluation"] = evaluation
        if expected == "invalid_regex":
            assert expected in _issue_codes(document)
        else:
            domain = _domain(document).conditions.events[0].evaluation
            operator, value, case_sensitive = expected
            assert domain.operator == operator
            assert domain.value == value
            assert domain.case_sensitive is case_sensitive

    for mutation, expected_code in (
        (
            lambda event: event.update(match_count=2, match_period=0),
            "invalid_match_window",
        ),
        (
            lambda event: event.update(instances=["SENSOR0:a", "SENSOR0:b"]),
            "duplicate_instance",
        ),
        (
            lambda event: event["evaluation"].update(value=[1.0, 2.0]),
            "instance_value_mismatch",
        ),
        (
            lambda event: event["path"].update(key=["KEY0", "KEY1"]),
            "instance_path_mismatch",
        ),
    ):
        document = _document()
        mutation(_event(document))
        assert expected_code in _issue_codes(document)


def test_instanced_comparison_event_identity_and_i2c_path_contracts():
    """Accept aligned instance data and reject duplicate event identities."""

    document = _document()
    event = _event(document)
    event["instances"] = ["SENSOR0:key0", "SENSOR1:key1"]
    event["path"]["key"] = ["KEY0", "KEY1"]
    event["evaluation"]["value"] = [1.0, 2.0]

    domain = _domain(document)

    assert domain.conditions.events[0].instances == (
        "SENSOR0:key0",
        "SENSOR1:key1",
    )

    # Conditions reject duplicate event identities.
    document = _document()
    conditions = document["signatures"][0]["signature"]["conditions"]
    conditions["events"].append(deepcopy(conditions["events"][0]))

    assert "duplicate_event_id" in _issue_codes(document)

    # I2C accepts both positional and scalar bus contracts.
    document = _document()
    _event(document).update(
        type="i2c",
        instances=["PSU0:bus0", "PSU1:bus1"],
        path={
            "bus": ["bus0", "bus1"],
            "chip_addr": "0x58",
            "i2c_type": "get",
            "command": "0x7A",
            "size": "b",
            "scaling": 0.5,
        },
        evaluation={"type": "mask", "logic": "&", "value": "0x80"},
    )

    event = _domain(document).conditions.events[0]

    assert event.path["bus"] == ("bus0", "bus1")
    assert event.path["scaling"] == 0.5
    assert event.evaluation.logic == "&"

    document = _document()
    _event(document).update(
        type="i2c",
        path={
            "bus": "bus0",
            "chip_addr": "0x58",
            "i2c_type": "get",
            "command": "0x7A",
            "size": "b",
        },
        evaluation={"type": "mask", "logic": "&", "value": 1},
    )

    assert _domain(document).conditions.events[0].path["bus"] == "bus0"


def test_source_paths_convert_reference_hook_and_optional_field_forms():
    reference_document = _document()
    _event(reference_document).update(
        type="platform_api", path="sensor:read_fault()"
    )
    assert (
        _domain(reference_document).conditions.events[0].path
        == "sensor:read_fault()"
    )

    hook_document = _document()
    _event(hook_document).update(
        type="platform_api",
        path={"hook": "read_fault", "channel": "A", "argv": ["ignored"]},
    )
    path = _domain(hook_document).conditions.events[0].path
    assert path == {"hook": "read_fault", "channel": "A", "argv": ("ignored",)}

    instanced_document = _document()
    _event(instanced_document).update(
        type="platform_api",
        instances=["SENSOR0:first", "SENSOR1:second"],
        path={
            "hook": "read_fault",
            "channels": ["first", "second"],
        },
    )
    assert _domain(instanced_document).conditions.events[0].path["channels"] == (
        "first",
        "second",
    )

    cli_document = _document()
    _event(cli_document).update(
        type="cli", path={"argv": ["/bin/echo", "1"], "timeout": 2}
    )
    assert _domain(cli_document).conditions.events[0].path == {
        "argv": ("/bin/echo", "1"),
        "timeout": 2,
    }

    file_document = _document()
    _event(file_document).update(
        type="file",
        path={
            "file": "/tmp/sensor",
            "format": "text",
            "scaling": 2,
            "unit": "C",
        },
    )
    assert _domain(file_document).conditions.events[0].path == {
        "file": "/tmp/sensor",
        "format": "text",
        "scaling": 2,
        "unit": "C",
    }

    defaults_document = _document()
    _event(defaults_document).update(
        type="file",
        path={"file": "/tmp/sensor", "format": "text"},
    )
    assert _domain(defaults_document).conditions.events[0].path == {
        "file": "/tmp/sensor",
        "format": "text",
    }

    cli_defaults_document = _document()
    _event(cli_defaults_document).update(
        type="cli", path={"argv": ["/bin/true"]}
    )
    assert _domain(cli_defaults_document).conditions.events[0].path == {
        "argv": ("/bin/true",)
    }


def test_boolean_and_dse_evaluations_convert_without_coercion():
    boolean_document = _document()
    _event(boolean_document)["evaluation"] = {
        "type": "boolean",
        "value": True,
    }
    boolean = _domain(boolean_document).conditions.events[0].evaluation
    assert boolean.type == "boolean"
    assert boolean.value is True

    dse_document = _document()
    _event(dse_document)["evaluation"] = {
        "type": "dse",
        "operator": "equals",
        "value": "sensor:threshold()",
    }
    dse = _domain(dse_document).conditions.events[0].evaluation
    assert dse.type == "dse"
    assert dse.operator == "equals"


def test_action_schema_discrimination_and_domain_conversion_fail_closed():
    """Enforce action values, vendor boundaries, discrimination, and conversion."""

    with pytest.raises(ValidationError):
        I2CActionPathV001.model_validate(
            {
                "bus": "1",
                "chip_addr": "0x58",
                "i2c_type": "set",
                "command": "0x7A",
                "size": "b",
            }
        )

    document = _document()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
    action.clear()
    action.update(
        type="i2c",
        timeout=5,
        path={
            "bus": "1",
            "chip_addr": "0x58",
            "i2c_type": "set",
            "command": "0x7A",
            "size": "b",
            "value": 0,
        },
    )

    operation = _domain(document).actions.repair_actions.local_actions.action_list[0]
    assert operation.path["value"] == 0

    document = _document()
    document["signatures"][0]["signature"]["actions"]["log_collection"] = {}
    assert "empty_log_collection" in _issue_codes(document)

    with pytest.raises(ValidationError):
        VendorOperationV001.model_validate({"type": "cli"})
    with pytest.raises(ValidationError):
        VendorOperationV001.model_validate(
            {"type": "platform-reset", "command": "unsafe"}
        )

    # Generated discrimination and domain converters reject unknown DTOs.
    schema = {}
    _vendor_operation_json_schema(schema)
    assert schema["allOf"][0]["not"]["anyOf"]

    assert _operation_discriminator(SimpleNamespace(type="cli")) == "cli"
    assert _operation_discriminator(SimpleNamespace(type=object())) == "vendor"

    invalid_dtos = (
        (_event_path_to_domain, object(), "event path model"),
        (
            _evaluation_to_domain,
            SimpleNamespace(type="unknown", value=1, value_configs=None),
            "evaluation model",
        ),
    )
    for converter, value, message in invalid_dtos:
        with pytest.raises(TypeError, match=message):
            converter(value)

    with pytest.raises(TypeError, match="operation model"):
        _operation_to_domain(
            SimpleNamespace(timeout=None),
            default_timeout=1,
            query=False,
            path="$.action",
        )
