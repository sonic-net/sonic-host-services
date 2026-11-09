from dataclasses import asdict
import json
from pathlib import Path
from typing import NoReturn

import pytest

from dldd.cli import main
from dldd.dse import DSEContext, DSEHook, DSEReference, DSERegistry
from dldd.models import Operation
from dldd.validation import ValidationContext, load_rules, validate_document
from tests.dldd_fakes import append_rule, load_valid_rules_document


class StaticOptionsHook(DSEHook):
    def resolve_source(self, reference: DSEReference, context: DSEContext) -> NoReturn:
        raise AssertionError("direct sources do not require DSE resolution")

    def resolve_evaluation(self, reference: DSEReference, context: DSEContext) -> NoReturn:
        raise AssertionError("direct evaluators do not require DSE resolution")

    def validate_vendor_operation(self, operation: Operation, context: DSEContext) -> None:
        if operation.options.get("target") != "supported":
            raise ValueError("vendor repair target is invalid")


def test_applicable_static_failure_rejects_all_rules_but_platform_filtering_does_not() -> None:
    document = load_valid_rules_document()
    second = append_rule(document, name="SECOND", rule_id=1000002)
    context = ValidationContext(
        product_id="PRODUCT-A", software_version="202311.3.0.1",
        require_compatibility_identity=True,
        dse_registry=DSERegistry(StaticOptionsHook(), action_types=("vendor_repair",)),
    )
    second["metadata"]["product_ids"] = ["OTHER-PRODUCT"]
    filtered = validate_document(document, context)
    assert filtered.activation_valid
    assert len(filtered.materialized_rules) == 1
    assert filtered.broken_rules[0].rule_id == 1000002

    second["metadata"]["product_ids"] = ["PRODUCT-A"]
    second["actions"]["repair_actions"]["local_actions"]["action_list"] = [{
        "action": {"type": "vendor_repair", "target": "invalid", "timeout": 1}
    }]
    rejected = validate_document(document, context)
    assert not rejected.file_valid
    assert rejected.ruleset is None
    assert rejected.materialized_rules == ()
    assert rejected.broken_rules[0].rule_id == 1000002
    assert rejected.broken_rules[0].issues[0].code == "materialization_failed"


def test_parser_failures_are_redacted_bounded_and_keep_safe_locations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    private_key = "PRIVATE_KEY_" + "x" * 2048
    sources = (
        ("{" + json.dumps(private_key) + ":1," + json.dumps(private_key) + ":2}", None),
        ('schema_version: "0.0.1"\nsecret: "PRIVATE_VALUE" trailing\n', 2),
    )
    for source, line in sources:
        result = load_rules(source)
        issue = result.file_errors[0]
        assert issue.code == "parse_error"
        assert issue.line == line
        assert len(issue.message.encode()) <= 1024
        assert "PRIVATE" not in json.dumps(asdict(issue))

        path = tmp_path / "invalid.yaml"
        path.write_text(source)
        assert main(["validate-rules", "--file", str(path), "--mode", "static-schema", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["file_errors"] == [asdict(issue)]
        assert "PRIVATE" not in json.dumps(payload)


def test_document_diagnostics_preserve_each_invalid_rule_and_its_truncation_marker() -> None:
    document = load_valid_rules_document()
    second = append_rule(document, name="SECOND", rule_id=1000002)
    first = document["signatures"][0]["signature"]
    first["metadata"].update({"unknown_{}".format(index): True for index in range(65)})
    second["metadata"]["unknown"] = True
    result = validate_document(document, materialize=False)

    assert not result.file_valid
    assert [rule.rule_id for rule in result.broken_rules] == [1000001, 1000002]
    assert sum(issue.code == "unknown_field" for issue in result.broken_rules[0].issues) == 64
    assert result.broken_rules[0].issues[-1].code == "validation_issues_truncated"
    assert result.broken_rules[0].issues[-1].path == "$.signatures[0]"
    assert result.broken_rules[1].issues[0].code == "unknown_field"
