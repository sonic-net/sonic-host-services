from __future__ import absolute_import

import json
import logging

import pytest

from dldd import validation
from dldd.dse import (
    DSEEvaluationHandle,
    DSEHook,
    DSERegistry,
    ResolvedEvaluation,
)
from dldd.models import ResolvedSource, ValueConfig
from dldd.validation import (
    CompatibilityMatcher,
    ValidationContext,
    load_document,
    load_rules,
    source_line_for_path,
    validate_document,
)
from tests.dldd_fakes import (
    load_valid_rules_document as _document,
    valid_rule_event as _event,
)


def test_compatibility_contract_requires_implementation_and_runtime_identity():
    with pytest.raises(NotImplementedError):
        CompatibilityMatcher.product_matches(None, "product", ())
    with pytest.raises(NotImplementedError):
        CompatibilityMatcher.software_matches(None, "version", ())

    missing = validate_document(
        _document(),
        ValidationContext(
            product_id="PRODUCT-A",
            require_compatibility_identity=True,
        ),
    )
    assert (
        "software version is unavailable"
        in missing.broken_rules[0].issues[0].message
    )

    mismatch = validate_document(
        _document(),
        ValidationContext(
            product_id="PRODUCT-A",
            software_version="not-supported",
        ),
    )
    assert "does not apply to software" in mismatch.broken_rules[0].issues[0].message


class InstancedEvaluationHook(DSEHook):
    def resolve_source(self, reference, context):
        raise AssertionError("direct source unexpectedly used DSE source resolution")

    def resolve_evaluation(self, reference, context):
        return DSEEvaluationHandle(
            reference,
            lambda invocation: ResolvedEvaluation(
                expected_value=10,
                operator=">",
            ),
        )


class DirectVendorSourceHook(DSEHook):
    def __init__(self):
        self.validated_sources = []

    def resolve_source(self, reference, context):
        raise AssertionError("direct source unexpectedly used DSE source resolution")

    def resolve_evaluation(self, reference, context):
        raise AssertionError("comparison unexpectedly used DSE evaluation resolution")

    def validate_resolved_source(self, source, context):
        self.validated_sources.append((source, context))


class CommonEvaluationHook(DirectVendorSourceHook):
    def __init__(self, runtime_handle):
        super().__init__()
        self.runtime_handle = runtime_handle

    def resolve_evaluation(self, reference, context):
        if self.runtime_handle:
            return DSEEvaluationHandle(
                reference,
                lambda invocation: ResolvedEvaluation(
                    expected_value=10,
                    operator=">",
                ),
            )
        return ResolvedEvaluation(
            expected_value=10,
            operator=">",
            value_configs=ValueConfig(type="int", unit="vendor"),
        )


class ReferenceSourceHook(DirectVendorSourceHook):
    def __init__(self):
        super().__init__()
        self.references = []

    def resolve_source(self, reference, context):
        self.references.append((reference, context))
        return (
            ResolvedSource(
                type="redis",
                path={
                    "database": "STATE_DB",
                    "table": "SENSOR_INFO",
                    "key": "SENSOR_INFO|0",
                    "path": "value",
                },
            ),
        )


def test_yaml_source_mapping_preserves_safe_keys_lines_and_alias_boundary(
    monkeypatch,
):
    pytest.importorskip("yaml")
    source = (
        "schema_version: '0.0.1'\n"
        "? [not, hashable]\n"
        ": value\n"
        "signatures: []\n"
    )

    result = load_rules(source)

    assert result.file_errors[0].code == "parse_error"
    assert "found unhashable key" in result.file_errors[0].message
    assert result.file_errors[0].line == 2

    source = (
        "schema_version: '0.0.1'\n"
        "signatures: []\n"
        "123: value\n"
    )

    result = load_rules(source)
    issue = next(item for item in result.file_errors if item.path == "$[123]")

    assert issue.line == 3
    assert result.source_lines["$[123]"] == 3

    source_lines = {"$": 1}

    assert source_line_for_path(source_lines, "") == 1
    assert source_line_for_path(source_lines, "unknown") == 1

    monkeypatch.setattr(validation, "MAX_YAML_ALIASES", 1)
    source = (
        "schema_version: &version '0.0.1'\n"
        "copied_version: *version\n"
        "signatures: []\n"
    )

    result = load_rules(source)

    assert result.file_errors[0].code != "parse_error"
    assert "aliases" not in result.file_errors[0].message


def test_source_and_document_scalar_boundaries_reject_untyped_or_oversized_data():
    class InvalidStream(object):
        def read(self, unused_limit):
            return object()

    result = load_rules(InvalidStream())

    assert result.file_errors[0].code == "parse_error"
    assert "text or bytes" in result.file_errors[0].message

    document = _document()
    document["vendor_extension"] = b"x" * (validation.MAX_SCALAR_BYTES + 1)

    result = validate_document(document, materialize=False)

    assert result.file_errors[0].code == "parse_error"
    assert "scalar larger" in result.file_errors[0].message


def test_file_rule_identity_and_diagnostic_budget_boundaries(monkeypatch):
    """Localize malformed identities and preserve bounded truncation markers."""

    for value in (None, 1, [], {}):
        document = _document()
        document["schema_version"] = value

        result = validate_document(document, materialize=False)

        assert [(issue.code, issue.path) for issue in result.file_errors] == [
            ("missing_schema_version", "$.schema_version")
        ], "schema_version={!r}".format(value)

    document = _document()
    document["signatures"][0]["signature"]["metadata"] = []

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert result.materialized_rules == ()
    assert result.broken_rules[0].rule_name == "signature[0]"
    assert result.broken_rules[0].rule_id is None
    assert result.broken_rules[0].issues[0].code == "invalid_type"

    document = _document()
    document["signatures"][0]["signature"]["metadata"]["name"] = 123

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert result.broken_rules[0].rule_name == "signature[0]"
    assert result.broken_rules[0].issues[0].code == "invalid_type"

    # File and rule issue budgets always retain their truncation marker.
    document = _document()
    for index in range(5):
        document["unknown_{}_{}".format(index, "x" * 400)] = True
    monkeypatch.setattr(validation, "MAX_ISSUES_PER_CANDIDATE", 2)
    monkeypatch.setattr(validation, "MAX_SERIALIZED_DIAGNOSTIC_BYTES", 512)

    result = validate_document(document, materialize=False)
    serialized = json.dumps(
        [
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in result.file_errors
        ],
        separators=(",", ":"),
    ).encode("utf-8")

    assert result.file_errors[-1].code == "validation_issues_truncated"
    assert len(result.file_errors) <= 2
    assert len(serialized) <= validation.MAX_SERIALIZED_DIAGNOSTIC_BYTES

    document = _document()
    metadata = document["signatures"][0]["signature"]["metadata"]
    for field in ("description", "severity", "component", "error_type"):
        metadata.pop(field)
    monkeypatch.setattr(validation, "MAX_ISSUES_PER_CANDIDATE", 2)

    result = validate_document(document, materialize=False)
    issues = result.broken_rules[0].issues

    assert len(issues) == 2
    assert issues[-1].code == "validation_issues_truncated"


def test_advertised_direct_vendor_source_requires_and_preflights_typed_hook():
    document = _document()
    _event(document).update(
        type="platform_api",
        path={"hook": "read_fault"},
    )

    result = validate_document(
        document,
        ValidationContext(
            dse_registry=DSERegistry(source_types=("platform_api",))
        ),
    )

    assert not result.activation_valid
    assert "requires an installed DSE hook" in result.broken_rules[0].issues[0].message

    document = _document()
    _event(document).update(
        type="platform_api",
        path={"hook": "read_fault"},
    )
    hook = DirectVendorSourceHook()

    result = validate_document(
        document,
        ValidationContext(
            dse_registry=DSERegistry(
                hook=hook,
                source_types=("platform_api",),
            )
        ),
    )

    assert result.activation_valid
    assert len(hook.validated_sources) == 1
    source, context = hook.validated_sources[0]
    assert source.type == "platform_api"
    assert context.rule_name == "PSU_OV_FAULT"


@pytest.mark.parametrize(
    "mode, event_type, path, evaluation",
    (
        pytest.param(
            "direct",
            "redis",
            {
                "database": "STATE_DB",
                "table": "SENSOR_INFO",
                "key": ["SENSOR_INFO|0", "SENSOR_INFO|1"],
                "path": "value",
            },
            None,
            id="direct-redis",
        ),
        pytest.param(
            "direct",
            "i2c",
            {
                "bus": ["bus0", "bus1"],
                "chip_addr": "0x58",
                "i2c_type": "get",
                "command": "0x7A",
                "size": "b",
            },
            {"type": "mask", "logic": "&", "value": 1},
            id="direct-i2c",
        ),
        pytest.param(
            "direct",
            "cli",
            {"argv": ["/bin/echo", "1"]},
            None,
            id="direct-cli",
        ),
        pytest.param(
            "direct",
            "file",
            {"file": "/tmp/value", "format": "text"},
            None,
            id="direct-file",
        ),
        pytest.param(
            "direct",
            "sysfs",
            {"file": "/sys/value", "format": "text"},
            None,
            id="direct-sysfs",
        ),
        pytest.param(
            "direct",
            "platform_api",
            {"hook": "read", "channels": ["first", "second"]},
            None,
            id="direct-platform-api",
        ),
        pytest.param(
            "reference", "dse", "sensor:read()", None, id="reference-dse"
        ),
        pytest.param(
            "reference",
            "platform_api",
            "sensor:read()",
            None,
            id="reference-platform-api",
        ),
    ),
)
def test_source_families_materialize_direct_mappings_or_dispatch_references(
    mode, event_type, path, evaluation
):
    document = _document()
    event = _event(document)
    if mode == "reference":
        event.update(type=event_type, path=path)
        hook = ReferenceSourceHook()
        result = validate_document(
            document,
            ValidationContext(dse_registry=DSERegistry(hook=hook)),
        )
        assert result.activation_valid
        assert [reference.canonical for reference, unused in hook.references] == [
            path
        ]
        assert result.materialized_rules[0].events[0].sources[0].type == "redis"
    else:
        event.update(
            type=event_type,
            path=path,
            instances=["SENSOR0:first", "SENSOR1:second"],
        )
        if evaluation is not None:
            event["evaluation"] = evaluation
        result = validate_document(document)
        assert result.activation_valid
        sources = result.materialized_rules[0].events[0].sources
        assert [source.instance for source in sources] == ["SENSOR0", "SENSOR1"]
        assert all(hasattr(source.path, "items") for source in sources)
        if event_type == "cli":
            assert sources[0].path["argv"] == ("/bin/echo", "1")


@pytest.mark.parametrize("mode", ("instanced", "common-runtime", "fixed"))
def test_dse_evaluator_mapping_supports_instanced_runtime_and_fixed_contracts(
    mode, caplog
):
    document = _document()
    evaluation = {"type": "dse", "value": "sensor:threshold()"}
    if mode == "instanced":
        evaluation["value"] = "{sensor*}:{threshold()}"
        hook = InstancedEvaluationHook()
    elif mode == "common-runtime":
        hook = CommonEvaluationHook(True)
    else:
        evaluation["value_configs"] = {"type": "float", "unit": "rule"}
        hook = CommonEvaluationHook(False)
    _event(document)["evaluation"] = evaluation

    with caplog.at_level(logging.WARNING, logger="dldd.validation"):
        result = validate_document(
            document,
            ValidationContext(dse_registry=DSERegistry(hook=hook)),
        )

    assert result.activation_valid
    assert not result.broken_rules
    materialized = result.materialized_rules[0].events[0]
    if mode == "instanced":
        assert materialized.dse_evaluation_handle
        assert [record.getMessage() for record in caplog.records] == [
            "rule 'PSU_OV_FAULT' event 1 applies instanced DSE evaluation selector "
            "'sensor*' to source bindings without explicit instances; allowing "
            "this unusual evaluator mapping"
        ]
    elif mode == "common-runtime":
        assert materialized.dse_evaluation_handle is not None
        assert materialized.sources[0].instance is None
    else:
        assert materialized.event.evaluation.value == 10
        assert materialized.event.evaluation.value_configs == ValueConfig(
            type="float", unit="rule"
        )


def test_document_loader_handles_inline_optional_and_invalid_source_boundaries(
    monkeypatch,
):
    with monkeypatch.context() as patch:
        patch.setattr(
            validation.os.path,
            "isfile",
            lambda unused_source: (_ for _ in ()).throw(OSError("bad path")),
        )
        result = load_rules("not a rules document")

    assert result.file_errors[0].code == "invalid_top_level"

    result = load_rules(object())

    assert result.file_errors[0].code == "parse_error"
    assert "source must be text" in result.file_errors[0].message

    document = _document()
    with monkeypatch.context() as patch:
        patch.setattr(validation, "yaml", None)
        result = load_rules(json.dumps(document), materialize=False)
        assert result.file_valid
        assert len(result.ruleset.signatures) == 1
        invalid = load_rules("not-json")
        assert invalid.file_errors[0].code == "parse_error"
        assert "PyYAML is unavailable" in invalid.file_errors[0].message

    pytest.importorskip("yaml")
    with monkeypatch.context() as patch:
        patch.setattr(
            validation.yaml,
            "compose",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("no marks")
            ),
        )
        result = load_rules(json.dumps(_document()), materialize=False)

    assert result.file_valid
    assert result.source_lines == {"$": 1}

    result = load_rules(b"\xff")

    assert result.file_errors[0].code == "parse_error"
    assert result.file_errors[0].line == 1

    document = _document()

    assert load_document(json.dumps(document)) == document
