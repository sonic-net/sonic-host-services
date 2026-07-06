from __future__ import absolute_import

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd.validation import load_rules


FIXTURE = Path(__file__).parent / "fixtures" / "valid-redis-rule.json"


def _document():
    return json.loads(FIXTURE.read_text())


def _line_containing(text, value):
    return next(
        index
        for index, line in enumerate(text.splitlines(), 1)
        if value in line
    )


def test_json_nested_validation_issue_has_exact_source_line():
    document = _document()
    evaluation = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]["evaluation"]
    evaluation["operator"] = "bogus"
    source = json.dumps(document, indent=2)

    result = load_rules(source, materialize=False)
    issue = next(
        item
        for item in result.broken_rules[0].issues
        if item.code == "unsupported_value"
    )

    assert issue.line == _line_containing(source, '"operator": "bogus"')


def test_yaml_missing_action_field_uses_nearest_parent_line():
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


def test_yaml_exotic_vendor_key_has_exact_source_line():
    yaml = pytest.importorskip("yaml")
    document = _document()
    action = document["signatures"][0]["signature"]["actions"][
        "repair_actions"
    ]["local_actions"]["action_list"][0]["action"]
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


@pytest.mark.parametrize(
    "source, expected_line",
    (
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
    ),
)
def test_parse_error_has_source_line(source, expected_line):
    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert result.file_errors[0].line == expected_line


def test_empty_source_uses_first_line_for_file_gate_error():
    result = load_rules("")

    assert result.file_errors[0].code == "invalid_top_level"
    assert result.file_errors[0].line == 1


def test_cli_json_and_text_output_include_issue_line(tmp_path, capsys):
    document = _document()
    evaluation = document["signatures"][0]["signature"]["conditions"][
        "events"
    ][0]["event"]["evaluation"]
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
