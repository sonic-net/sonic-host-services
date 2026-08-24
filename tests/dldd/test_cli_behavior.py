from __future__ import absolute_import

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookError, VendorHookRegistry
from dldd.models import BrokenRule, ValidationIssue
from dldd.platform import PlatformIdentity


FIXTURES = Path(__file__).parent / "fixtures"


def _args(mode, **overrides):
    values = {
        "mode": mode,
        "platform_dir": None,
        "dse": None,
        "file": "rules.yaml",
        "json": True,
        "verbose": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _extensions():
    return SimpleNamespace(
        dse_registry=DSERegistry(),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=SimpleNamespace(),
    )


def _materialized_rule(rule_id=1000001, name="CLI_RULE"):
    return SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(id=rule_id, name=name),
        ),
        events=(SimpleNamespace(),),
    )


def _validation(materialized=(), **overrides):
    values = {
        "schema_version": "0.0.1",
        "ruleset": None,
        "materialized_rules": tuple(materialized),
        "broken_rules": (),
        "file_errors": (),
        "file_valid": True,
        "activation_valid": True,
        "source_lines": {"$": 1, "$.signatures": 2},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _configure_runtime_validation(monkeypatch, result, preflight):
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: PlatformIdentity("test-platform", "product", "software"),
    )
    monkeypatch.setattr(
        dldd_cli, "load_extensions", lambda *unused_args: _extensions()
    )
    monkeypatch.setattr(
        dldd_cli,
        "load_rules",
        lambda *unused_args, **unused_kwargs: result,
    )
    monkeypatch.setattr(
        dldd_cli,
        "preflight_activation",
        lambda *unused_args, **unused_kwargs: preflight,
    )


def test_static_schema_validation_needs_no_platform_runtime(monkeypatch, capsys):
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: pytest.fail("static validation detected live platform identity"),
    )
    monkeypatch.setattr(
        dldd_cli,
        "load_extensions",
        lambda *unused_args: pytest.fail("static validation loaded extensions"),
    )

    status = dldd_cli.validate_rules(
        _args(
            "static-schema",
            file=str(FIXTURES / "valid-redis-rule.json"),
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["file_level_result"] == "PASSED"
    assert payload["rules_parsed_successfully"] == 1
    assert "probe_results" not in payload


@pytest.mark.parametrize(
    "failure,expected_status,expected_state,expected_code",
    (
        (None, 0, "AVAILABLE", None),
        (ValueError("bad live binding"), 1, "FAILED", "adapter_validation_failed"),
        (RuntimeError("sensor transport failed"), 1, "FAILED", "adapter_probe_failed"),
    ),
)
def test_hardware_probe_reports_live_availability_and_failures(
    monkeypatch,
    capsys,
    failure,
    expected_status,
    expected_state,
    expected_code,
):
    rule = _materialized_rule()
    result = _validation((rule,))
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        correlation_key="1000001:1:SENSOR0",
    )

    class LiveAdapter(object):
        def get_value(self, received):
            assert received is item
            if failure is not None:
                raise failure
            return "42"

    preflight = SimpleNamespace(
        adapters={"redis": LiveAdapter()},
        plan=SimpleNamespace(work_items={item.correlation_key: item}),
        invalid_rule_ids=frozenset(),
        failures=(),
    )
    _configure_runtime_validation(monkeypatch, result, preflight)

    status = dldd_cli.validate_rules(_args("hardware-probe"))
    payload = json.loads(capsys.readouterr().out)

    assert status == expected_status
    assert payload["probe_results"][0]["state"] == expected_state
    if expected_code is None:
        assert payload["broken_rules"] == []
    else:
        issue = payload["broken_rules"][0]["issues"][0]
        assert issue["code"] == expected_code
        assert payload["rules_parsed_successfully"] == 0


def test_preflight_rejection_skip_and_json_output_contract(
    monkeypatch, capsys
):
    rule = _materialized_rule(name="REJECTED_RULE")
    result = _validation((rule,))
    failure = SimpleNamespace(
        correlation_key="1000001:preflight",
        rule_name="REJECTED_RULE",
        rule_id=1000001,
        code="activation_preflight_failed",
        message="unsupported live source",
    )
    preflight = SimpleNamespace(
        adapters={},
        plan=SimpleNamespace(work_items={}, templates={}, signatures={}),
        invalid_rule_ids=frozenset((1000001,)),
        failures=(failure,),
    )
    _configure_runtime_validation(monkeypatch, result, preflight)
    monkeypatch.setattr(
        dldd_cli,
        "qualify_e2e",
        lambda *unused_args: SimpleNamespace(
            event_results=(), rule_results=(), failed=False
        ),
    )

    status = dldd_cli.validate_rules(_args("e2e-execute"))
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["qualification_result"] == "FAILED"
    assert payload["probe_results"] == [
        {
            "component": None,
            "correlation_key": "1000001:preflight",
            "error": "unsupported live source",
            "event_id": None,
            "rule": "REJECTED_RULE",
            "rule_id": 1000001,
            "stage": "preflight",
            "state": "FAILED",
        }
    ]
    issue = payload["broken_rules"][0]["issues"][0]
    assert issue["code"] == "activation_preflight_failed"
    assert issue["line"] == 2

    status = dldd_cli.validate_rules(
        _args("activation-dry-run", json=False)
    )
    output = capsys.readouterr().out

    assert status == 1
    assert "Rules failed validation: 1" in output
    assert "activation_preflight_failed" in output
    assert "unsupported live source" in output
    assert "(line 2)" in output


    rule = _materialized_rule(name="REJECTED_RULE")
    result = _validation((rule,))
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        correlation_key="1000001:rejected",
    )

    class MustNotProbeAdapter(object):
        def get_value(self, unused_item):
            pytest.fail("hardware probe read a preflight-rejected work item")

    failure = SimpleNamespace(
        correlation_key=item.correlation_key,
        rule_name="REJECTED_RULE",
        rule_id=1000001,
        code="adapter_validation_failed",
        message="source binding is unsupported",
    )
    preflight = SimpleNamespace(
        adapters={"redis": MustNotProbeAdapter()},
        plan=SimpleNamespace(work_items={item.correlation_key: item}),
        invalid_rule_ids=frozenset((1000001,)),
        failures=(failure,),
    )
    _configure_runtime_validation(monkeypatch, result, preflight)

    status = dldd_cli.validate_rules(_args("hardware-probe"))
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["probe_results"] == [
        {
            "correlation_key": item.correlation_key,
            "error": "source binding is unsupported",
            "state": "FAILED",
        }
    ]
    assert payload["rules_parsed_successfully"] == 0


def test_human_validation_and_e2e_output_rendering_contract(
    monkeypatch, capsys
):
    broken = BrokenRule(
        rule_name="BROKEN_RULE",
        rule_id=1000001,
        rule_version="1.0.0",
        issues=(
            ValidationIssue(
                scope="rule",
                code="missing_field",
                message="severity is required",
                path="$.signatures[0].metadata.severity",
                line=7,
            ),
        ),
    )
    file_issue = ValidationIssue(
        scope="file",
        code="invalid_document",
        message="document must be an object",
        path="$",
    )
    result = _validation(
        broken_rules=(broken,),
        file_errors=(file_issue,),
        file_valid=False,
        activation_valid=False,
    )
    _configure_runtime_validation(
        monkeypatch,
        result,
        SimpleNamespace(),
    )

    status = dldd_cli.validate_rules(
        _args("dse-resolve", json=False, platform_dir="/platform", dse="/dse")
    )
    output = capsys.readouterr().out

    assert status == 1
    assert "$.signatures[0].metadata.severity (line 7)" in output
    assert "file: $: document must be an object" in output
    assert "Rule-level result: FAILED" in output

    rule = _materialized_rule(name="VERBOSE_RULE")
    result = _validation((rule,))
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        correlation_key="1000001:verbose",
    )
    preflight = SimpleNamespace(
        adapters={"redis": object()},
        plan=SimpleNamespace(work_items={item.correlation_key: item}),
        invalid_rule_ids=frozenset(),
        failures=(),
    )
    _configure_runtime_validation(monkeypatch, result, preflight)

    status = dldd_cli.validate_rules(
        _args("activation-dry-run", json=False, verbose=True)
    )
    output = capsys.readouterr().out

    assert status == 0
    assert "materialized VERBOSE_RULE with 1 event(s)" in output
    assert "probe 1000001:verbose: VALID" in output

    rule = _materialized_rule(name="E2E_RULE")
    result = _validation((rule,))
    preflight = SimpleNamespace(
        adapters={},
        plan=SimpleNamespace(work_items={}, templates={}, signatures={}),
        invalid_rule_ids=frozenset(),
        failures=(),
    )
    _configure_runtime_validation(monkeypatch, result, preflight)
    monkeypatch.setattr(
        dldd_cli,
        "qualify_e2e",
        lambda *unused_args: SimpleNamespace(
            event_results=(
                {
                    "rule": "E2E_RULE",
                    "event_id": 1,
                    "component": "SENSOR0",
                    "stage": "collection",
                    "state": "MATCH",
                },
                {
                    "correlation_key": "unresolved",
                    "stage": "expansion",
                    "state": "UNQUALIFIED",
                    "reason": "no instances discovered",
                },
            ),
            rule_results=(
                {
                    "rule": "E2E_RULE",
                    "component": "SENSOR0",
                    "stage": "rule_logic",
                    "state": "NO_MATCH",
                },
                {
                    "rule": "E2E_RULE",
                    "component": None,
                    "stage": "rule_logic",
                    "state": "UNQUALIFIED",
                    "reason": "no instances discovered",
                },
            ),
            failed=True,
        ),
    )

    status = dldd_cli.validate_rules(_args("e2e-execute", json=False))
    output = capsys.readouterr().out

    assert status == 1
    assert "Qualification result: FAILED" in output
    assert "event E2E_RULE:1:SENSOR0 [collection]: MATCH" in output
    assert "event unknown:-:unresolved [expansion]: UNQUALIFIED" in output
    assert "(no instances discovered)" in output
    assert "rule E2E_RULE:SENSOR0 [rule_logic]: NO_MATCH" in output


def test_clear_state_and_main_dispatch_contract(monkeypatch, capsys, caplog):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(dldd_cli.subprocess, "run", run)
    monkeypatch.setattr(dldd_cli, "SonicStateDB", lambda: "state-db")
    monkeypatch.setattr(
        dldd_cli,
        "clear_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(
            redis_keys=1, faults=0, artifacts=0
        ),
    )

    assert dldd_cli.clear_state(SimpleNamespace(all=False)) == 0
    assert calls == [
        ["/bin/systemctl", "is-active", "--quiet", "dldd.service"]
    ]
    assert "1 Redis key(s), 0 fault(s)" in capsys.readouterr().out

    calls = []
    cleared = []

    def run_active(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dldd_cli.subprocess, "run", run_active)
    monkeypatch.setattr(
        dldd_cli,
        "clear_runtime_state",
        lambda state_db, state_file, **kwargs: (
            cleared.append((state_db, state_file, kwargs))
            or SimpleNamespace(redis_keys=7, faults=2, artifacts=3)
        ),
    )

    assert dldd_cli.clear_state(SimpleNamespace(all=True)) == 0
    assert calls == [
        ["/bin/systemctl", "is-active", "--quiet", "dldd.service"],
        ["/bin/systemctl", "stop", "dldd.service"],
        ["/bin/systemctl", "start", "dldd.service"],
    ]
    assert cleared == [
        (
            "state-db",
            "/var/lib/sonic/dld_state.json",
            {"include_faults": True, "include_artifacts": True},
        )
    ]

    for failure_stage in ("stop", "clear", "restart"):
        calls = []

        def run(command, **kwargs):
            calls.append(command[1])
            failed_command = (
                "start" if failure_stage == "restart" else failure_stage
            )
            if command[1] == failed_command:
                raise RuntimeError("{} failed".format(failure_stage))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(dldd_cli.subprocess, "run", run)
        monkeypatch.setattr(dldd_cli, "SonicStateDB", lambda: "state-db")

        def clear(*unused_args, **unused_kwargs):
            if failure_stage == "clear":
                raise RuntimeError("clear failed")
            return SimpleNamespace(redis_keys=1, faults=0, artifacts=0)

        monkeypatch.setattr(dldd_cli, "clear_runtime_state", clear)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="dldd.cli"):
            status = dldd_cli.clear_state(SimpleNamespace(all=True))

        assert status == 1
        assert "{} failed".format(failure_stage) in caplog.text
        if failure_stage == "stop":
            assert calls == ["is-active", "stop"]
        else:
            assert calls == ["is-active", "stop", "start"]


    for argv, target in (
        (["validate-rules", "--file", "rules.yaml"], "validate"),
        (["clear-state"], "clear"),
        (["run"], "run"),
        ([], "run"),
    ):
        calls = []
        monkeypatch.setattr(
            dldd_cli,
            "validate_rules",
            lambda args: calls.append(("validate", args.file)) or 7,
        )
        monkeypatch.setattr(
            dldd_cli,
            "clear_state",
            lambda args: calls.append(("clear", args.all)) or 8,
        )
        monkeypatch.setattr(
            dldd_cli,
            "run_service",
            lambda: calls.append(("run", None)),
        )

        status = dldd_cli.main(argv)

        if target == "validate":
            assert status == 7
            assert calls == [("validate", "rules.yaml")]
        elif target == "clear":
            assert status == 8
            assert calls == [("clear", False)]
        else:
            assert status == 0
            assert calls == [("run", None)]
