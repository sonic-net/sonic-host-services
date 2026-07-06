import json
from pathlib import Path
import re

import pytest
from pydantic import ValidationError

from dldd.rule_schema.base import INT64_MIN, UINT64_MAX
from dldd.rule_schema.generate import generate_schema
from dldd.rule_schema.v0_0_1 import (
    ComparisonEvaluationV001,
    MaskEvaluationV001,
)


def _number_branches(value, path="$"):
    branches = []
    if isinstance(value, dict):
        if value.get("type") == "number":
            branches.append((path, value))
        for key, child in value.items():
            branches.extend(_number_branches(child, "{}.{}".format(path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            branches.extend(
                _number_branches(child, "{}[{}]".format(path, index))
            )
    return branches


def test_generated_float_branches_publish_runtime_type_limitation():
    branches = _number_branches(generate_schema("0.0.1"))

    assert branches
    for unused_path, branch in branches:
        assert "not" not in branch
        constraint = branch["x-dldd-runtime-constraint"]
        assert constraint["authority"] == "pydantic-runtime-contract"
        assert constraint["kind"] == "python-float-type"


def test_draft_2020_derivative_accepts_integral_valued_float_fixture():
    jsonschema = pytest.importorskip("jsonschema")
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "valid-redis-rule.json"
        ).read_text()
    )
    event = fixture["signatures"][0]["signature"]["conditions"]["events"][
        0
    ]["event"]
    event["evaluation"] = {
        "type": "comparison",
        "operator": "==",
        "value": 50.0,
    }

    jsonschema.Draft202012Validator(generate_schema("0.0.1")).validate(
        fixture
    )


@pytest.mark.parametrize("value", [INT64_MIN - 1, UINT64_MAX + 1])
def test_out_of_range_integer_cannot_fall_through_float_branch(value):
    with pytest.raises(ValidationError):
        ComparisonEvaluationV001.model_validate(
            {"type": "comparison", "operator": "==", "value": value}
        )

    assert ComparisonEvaluationV001.model_validate(
        {"type": "comparison", "operator": "==", "value": 1.5}
    ).value == 1.5


@pytest.mark.parametrize(
    "value",
    [
        str(INT64_MIN - 1),
        str(UINT64_MAX + 1),
        "0x10000000000000000",
    ],
)
def test_mask_string_range_annotation_defers_to_runtime_authority(value):
    schema = generate_schema("0.0.1")
    options = schema["$defs"]["MaskEvaluationV001"]["properties"]["value"][
        "anyOf"
    ]
    string_branch = next(option for option in options if option["type"] == "string")
    constraint = string_branch["x-dldd-runtime-constraint"]

    assert constraint["authority"] == "pydantic-runtime-contract"
    assert constraint["kind"] == "parsed-integer-range"
    assert constraint["minimum"] == INT64_MIN
    assert constraint["maximum"] == UINT64_MAX
    # JSON Schema cannot apply numeric bounds after parsing a string.  The
    # derivative publishes the syntactic pattern plus an explicit semantic
    # annotation, while the installed Pydantic validator remains authoritative.
    assert re.fullmatch(string_branch["pattern"], value)
    with pytest.raises(ValidationError):
        MaskEvaluationV001.model_validate(
            {"type": "mask", "logic": "&", "value": value}
        )


def test_mask_string_runtime_accepts_published_range_boundaries():
    for value in (str(INT64_MIN), str(UINT64_MAX)):
        validated = MaskEvaluationV001.model_validate(
            {"type": "mask", "logic": "&", "value": value}
        )
        assert validated.value == value
