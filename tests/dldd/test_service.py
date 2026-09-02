from __future__ import absolute_import

import json
from types import SimpleNamespace

from dldd import service as dldd_service
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.models import BrokenRule, ValidationIssue, ValidationResult
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService, TelemetryUnavailable
from dldd.validation import ExactCompatibilityMatcher


def _service(tmp_path):
    return DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=PlatformExtensions(
            PlatformIdentity("test", "product", "software"),
            DSERegistry(),
            VendorHookRegistry(),
            ExactCompatibilityMatcher(),
        ),
    )


def test_operator_status_uses_public_identity_without_correlation_keys():
    records = (
        {"correlation_key": "work-1", "reason": "runtime failure"},
        {
            "correlation_key": "rule:1000002",
            "rule_id": 1000002,
            "reason": "ingestion failure",
        },
    )
    work_items = {
        "work-1": SimpleNamespace(
            rule_id=1000001,
            component_type="PSU",
            component_name="PSU1",
        )
    }

    projected = dldd_service._operator_status_records(records, work_items)

    assert projected == (
        {
            "reason": "runtime failure",
            "rule_id": 1000001,
            "component_type": "PSU",
            "component_name": "PSU1",
            "rule_instance_id": "1000001@PSU1",
        },
        {
            "rule_id": 1000002,
            "reason": "ingestion failure",
        },
    )
    assert all("correlation_key" not in record for record in projected)


def test_candidate_localizes_invalid_rules_and_bounds_external_diagnostics(
    tmp_path, monkeypatch
):
    broken_rule = BrokenRule(
        rule_name="BAD_RULE",
        rule_id=1000001,
        rule_version="2.3.4",
        issues=(
            ValidationIssue("rule", "missing_field", "severity is required"),
        ),
    )
    validation = SimpleNamespace(
        schema_version="0.0.1",
        materialized_rules=(),
        broken_rules=(broken_rule,),
        file_errors=(),
        file_valid=True,
    )
    service = _service(tmp_path)
    monkeypatch.setattr(dldd_service, "load_rules", lambda *args: validation)
    monkeypatch.setattr(dldd_service.time, "time", lambda: 1234.5)

    candidate = service._validate_candidate("rules.yaml", "dse.yaml")

    assert candidate.broken_rules[0] == {
        "rule": "BAD_RULE",
        "rule_id": 1000001,
        "version": "2.3.4",
        "state": "BROKEN",
        "reason": "schema_error: $: severity is required (missing_field)",
        "failure_count": 1,
        "last_attempt": 1234,
    }

    oversized = tuple(
        BrokenRule(
            rule_name="\0" * 240 + "{:04d}".format(index),
            rule_id=1_000_000 + index,
            rule_version="1.0.0",
            issues=(
                ValidationIssue(
                    "rule",
                    "unknown_field",
                    "X" * 4096,
                    path="$." + "Y" * 2048,
                ),
            ),
        )
        for index in range(1024)
    )
    records = dldd_service._bounded_broken_rule_records(
        SimpleNamespace(broken_rules=oversized), 1234.5
    )
    serialized = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert len(records) == 1024
    assert len(serialized) <= dldd_service.MAX_SERIALIZED_DIAGNOSTIC_BYTES
    assert len({record["rule"] for record in records}) == 1024
    assert all("\0" not in record["rule"] for record in records)


def test_candidate_keeps_valid_rules_when_activation_preflight_rejects_one(
    tmp_path, monkeypatch
):
    accepted = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(
                id=1000001, name="ACCEPTED", version="1.0.0"
            )
        )
    )
    rejected = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(
                id=1000002, name="REJECTED", version="2.0.0"
            )
        )
    )
    validation = ValidationResult(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(accepted, rejected),
        source_lines={"$": 1, "$.signatures": 7},
    )
    service = _service(tmp_path)
    monkeypatch.setattr(dldd_service, "load_rules", lambda *args: validation)
    monkeypatch.setattr(
        dldd_service,
        "preflight_activation",
        lambda *args, **kwargs: SimpleNamespace(
            failures=(
                SimpleNamespace(
                    rule_id=1000002,
                    rule_name="REJECTED",
                    message="unsupported source binding",
                ),
            )
        ),
    )
    monkeypatch.setattr(dldd_service.time, "time", lambda: 6789.0)

    candidate = service._validate_candidate("rules.yaml", "dse.yaml")

    assert candidate.activatable
    assert candidate.payload.materialized_rules == (accepted,)
    assert candidate.broken_rules[0]["rule_id"] == 1000002
    assert "unsupported source binding" in candidate.broken_rules[0]["reason"]


def test_run_stops_uncleanly_after_three_status_publication_failures(
    monkeypatch,
):
    service = object.__new__(DLDDService)
    service.orchestrator = None
    service.stop_event = SimpleNamespace(
        is_set=lambda: False,
        wait=lambda timeout: False,
    )
    service.start = lambda: None
    service._publish_status = lambda: False
    shutdown_modes = []
    service.shutdown = lambda clean_shutdown=True: shutdown_modes.append(
        clean_shutdown
    )
    clock = iter((0.0, 0.0, 1.0, 2.0))
    monkeypatch.setattr(dldd_service.time, "monotonic", lambda: next(clock))

    try:
        service.run()
    except TelemetryUnavailable as error:
        assert "failed 3 consecutive times" in str(error)
    else:
        raise AssertionError("status publication failures did not stop DLDD")

    assert shutdown_modes == [False]
