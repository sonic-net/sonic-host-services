from __future__ import absolute_import

import json

import pytest

from dldd.validation import load_rules
from tests.dldd_fakes import (
    load_valid_rules_document as _document,
    valid_rule_event,
)


def _line_containing(text, value):
    return next(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if value in line
    )


def test_nested_json_and_missing_yaml_fields_use_source_lines():
    """Resolve exact and nearest-parent lines across JSON and YAML paths."""

    document = _document()
    evaluation = valid_rule_event(document)["evaluation"]
    evaluation["operator"] = "bogus"
    source = json.dumps(document, indent=2)

    result = load_rules(source, materialize=False)
    issue = next(
        item
        for item in result.broken_rules[0].issues
        if item.code == "unsupported_value"
    )

    assert issue.line == _line_containing(source, '"operator": "bogus"')

    # Missing YAML fields use the closest materialized parent line.
    yaml = pytest.importorskip("yaml")
    document = _document()
    del document["local_action_default_timeout"]
    source = yaml.safe_dump(document, sort_keys=False)

    result = load_rules(source, materialize=False)
    issue = next(
        item
        for item in result.broken_rules[0].issues
        if item.code == "missing_action_timeout"
    )

    assert issue.line == _line_containing(source, "- action:")


def test_parse_errors_use_stable_source_lines():
    for source, expected_line in (
        (
            "schema_version: '0.0.1'\n"
            "signatures:\n"
            "  - !!python/object/apply:os.system ['echo unsafe']\n",
            3,
        ),
        (
            "{\n"
            '  "schema_version": "0.0.1",\n'
            '  "signatures": [\n'
            "}\n",
            4,
        ),
    ):
        result = load_rules(source)
        assert result.file_errors[0].code == "parse_error"
        assert result.file_errors[0].line == expected_line
