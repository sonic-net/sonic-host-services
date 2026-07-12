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


def test_generated_schema_and_runtime_numeric_authority_are_aligned():
    """Keep authoring annotations aligned with Pydantic numeric execution."""

    schema = generate_schema("0.0.1")
    branches = _number_branches(schema)

    assert branches
    for path, branch in branches:
        assert "not" not in branch, "numeric branch {}".format(path)
        constraint = branch["x-dldd-runtime-constraint"]
        assert constraint["authority"] == "pydantic-runtime-contract", path
        assert constraint["kind"] == "python-float-type", path

    for kind, value in (
        ("comparison", INT64_MIN - 1),
        ("comparison", UINT64_MAX + 1),
        ("mask", str(INT64_MIN - 1)),
        ("mask", str(UINT64_MAX + 1)),
        ("mask", "0x10000000000000000"),
    ):
        if kind == "comparison":
            with pytest.raises(ValidationError):
                ComparisonEvaluationV001.model_validate(
                    {"type": "comparison", "operator": "==", "value": value}
                )
        else:
            schema = generate_schema("0.0.1")
            options = schema["$defs"]["MaskEvaluationV001"]["properties"][
                "value"
            ]["anyOf"]
            string_branch = next(
                option for option in options if option["type"] == "string"
            )
            assert re.fullmatch(string_branch["pattern"], value)
            with pytest.raises(ValidationError):
                MaskEvaluationV001.model_validate(
                    {"type": "mask", "logic": "&", "value": value}
                )

    assert ComparisonEvaluationV001.model_validate(
        {"type": "comparison", "operator": "==", "value": 1.5}
    ).value == 1.5
    for value in (str(INT64_MIN), str(UINT64_MAX)):
        validated = MaskEvaluationV001.model_validate(
            {"type": "mask", "logic": "&", "value": value}
        )
        assert validated.value == value

    # Optional authoring-schema validation runs only after runtime parity, so a
    # missing jsonschema dependency cannot skip the executable contract checks.
    jsonschema = pytest.importorskip("jsonschema")
    options = schema["$defs"]["MaskEvaluationV001"]["properties"]["value"][
        "anyOf"
    ]
    string_branch = next(
        option for option in options if option["type"] == "string"
    )
    constraint = string_branch["x-dldd-runtime-constraint"]
    assert {
        key: constraint[key]
        for key in ("authority", "kind", "minimum", "maximum")
    } == {
        "authority": "pydantic-runtime-contract",
        "kind": "parsed-integer-range",
        "minimum": INT64_MIN,
        "maximum": UINT64_MAX,
    }
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
    jsonschema.Draft202012Validator(schema).validate(fixture)
