from __future__ import absolute_import

import json
from datetime import date
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd.validation import load_rules
from tests.dldd_fakes import (
    load_valid_rules_document as _document,
    valid_rule_action,
    valid_rule_event,
)


def _line_containing(text, value):
    return next(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if value in line
    )


def test_nested_json_yaml_and_vendor_issues_use_exact_source_lines():
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

    # Exotic vendor keys retain their exact YAML line.
    yaml = pytest.importorskip("yaml")
    document = _document()
    action = valid_rule_action(document)
    action.clear()
    action.update(
        {
            "type": "acme_psu_reset",
            "payload": {"x.y": date(2026, 7, 6)},
        }
    )
    source = yaml.safe_dump(document, sort_keys=False)

    result = load_rules(source, materialize=False)
    issue = next(
        item
        for item in result.broken_rules[0].issues
        if item.path.endswith('payload["x.y"]')
    )

    assert issue.line == _line_containing(source, "x.y:")

    # Non-string keys retain a stable JSONPath and source line.
    result = load_rules(
        "schema_version: '0.0.1'\n"
        "signatures: []\n"
        "123: value\n"
    )
    issue = next(item for item in result.file_errors if item.path == "$[123]")
    assert issue.line == 3
    assert result.source_lines["$[123]"] == 3


def test_parse_and_empty_file_errors_use_stable_source_lines():
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

    # An empty source reports its file gate at the first line.
    result = load_rules("")

    assert result.file_errors[0].code == "invalid_top_level"
    assert result.file_errors[0].line == 1


def test_cli_json_and_text_output_include_issue_line(tmp_path, capsys):
    document = _document()
    evaluation = valid_rule_event(document)["evaluation"]
    evaluation["operator"] = "bogus"
    source = json.dumps(document, indent=2)
    expected_line = _line_containing(source, '"operator": "bogus"')
    path = tmp_path / "rules.json"
    path.write_text(source)
    args = SimpleNamespace(
        mode="static-schema",
        platform_dir=None,
        dse=None,
        file=str(path),
        json=True,
        verbose=False,
    )

    assert dldd_cli.validate_rules(args) == 1
    payload = json.loads(capsys.readouterr().out)
    issue = payload["broken_rules"][0]["issues"][0]
    assert issue["line"] == expected_line

    args.json = False
    assert dldd_cli.validate_rules(args) == 1
    output = capsys.readouterr().out
    assert "(line {})".format(expected_line) in output
