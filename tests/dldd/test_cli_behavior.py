from __future__ import absolute_import

import json
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookRegistry
from dldd.models import ValidationResult
from dldd.platform import PlatformIdentity
from dldd.preflight import (
    ActivationPreflightFailure,
    ActivationPreflightResult,
    validation_with_preflight_failures,
)


def _validation(materialized):
    return ValidationResult(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=tuple(materialized),
        broken_rules=(),
        file_errors=(),
        source_lines={"$": 1, "$.signatures": 2},
    )


def _rule(rule_id, name):
    return SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(id=rule_id, name=name),
        ),
        events=(SimpleNamespace(),),
    )


def test_hardware_probe_skips_preflight_rejections_and_orders_failures(
    monkeypatch, capsys
):
    live_rule = _rule(1000001, "LIVE_RULE")
    rejected_rule = _rule(1000002, "REJECTED_RULE")
    validation = _validation((rejected_rule, live_rule))
    rejected = SimpleNamespace(
        source_type="redis",
        rule_id=1000002,
        correlation_key="1000002:rejected",
    )
    live = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        correlation_key="1000001:live",
    )

    class LiveAdapter(object):
        def get_value(self, item):
            if item is rejected:
                pytest.fail("hardware probe read a preflight-rejected item")
            assert item is live
            raise RuntimeError("sensor transport failed")

    failure = ActivationPreflightFailure(
        1000002,
        "REJECTED_RULE",
        "1.0.0",
        rejected.correlation_key,
        "adapter_validation_failed",
        "source binding is unsupported",
    )
    preflight = ActivationPreflightResult(
        validation=validation_with_preflight_failures(validation, (failure,)),
        adapters={"redis": LiveAdapter()},
        plan=SimpleNamespace(
            work_items={
                rejected.correlation_key: rejected,
                live.correlation_key: live,
            }
        ),
        failures=(failure,),
    )
    extensions = SimpleNamespace(
        dse_registry=DSERegistry(),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=SimpleNamespace(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: PlatformIdentity("test-platform", "product", "software"),
    )
    monkeypatch.setattr(
        dldd_cli, "load_extensions", lambda *unused_args: extensions
    )
    monkeypatch.setattr(
        dldd_cli,
        "load_rules",
        lambda *unused_args, **unused_kwargs: validation,
    )
    monkeypatch.setattr(
        dldd_cli,
        "preflight_activation",
        lambda *unused_args, **unused_kwargs: preflight,
    )
    args = SimpleNamespace(
        mode="hardware-probe",
        platform_dir=None,
        dse=None,
        file="rules.yaml",
        json=True,
        verbose=False,
    )

    status = dldd_cli.validate_rules(args)
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["probe_results"] == [
        {
            "correlation_key": rejected.correlation_key,
            "error": "source binding is unsupported",
            "state": "FAILED",
        },
        {
            "correlation_key": live.correlation_key,
            "error": "sensor transport failed",
            "state": "FAILED",
        },
    ]
    assert [item["rule_id"] for item in payload["broken_rules"]] == [
        1000001,
        1000002,
    ]
    assert [item["issues"][0]["code"] for item in payload["broken_rules"]] == [
        "adapter_probe_failed",
        "adapter_validation_failed",
    ]


def test_clear_state_stops_and_restarts_an_active_service(monkeypatch):
    calls = []
    cleared = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dldd_cli.subprocess, "run", run)
    monkeypatch.setattr(dldd_cli, "SonicStateDB", lambda: "state-db")
    monkeypatch.setattr(
        dldd_cli,
        "clear_runtime_state",
        lambda state_db, state_file, **kwargs: (
            cleared.append((state_db, state_file, kwargs))
            or SimpleNamespace(redis_keys=7, faults=2, artifacts=3)
        ),
    )

    assert dldd_cli.clear_state(SimpleNamespace(all=True)) == 0
    assert [command[1] for command in calls] == [
        "is-active",
        "stop",
        "start",
    ]
    assert cleared == [
        (
            "state-db",
            "/var/lib/sonic/dld_state.json",
            {"include_faults": True, "include_artifacts": True},
        )
    ]

    calls.clear()
    cleared.clear()
    monkeypatch.setattr(
        dldd_cli.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or SimpleNamespace(returncode=3)
        ),
    )

    assert dldd_cli.clear_state(SimpleNamespace(all=False)) == 0
    assert [command[1] for command in calls] == ["is-active"]
    assert cleared[0][2] == {
        "include_faults": False,
        "include_artifacts": False,
    }


@pytest.mark.parametrize(
    "argv,target,status",
    (
        (["validate-rules", "--file", "rules.yaml"], "validate", 7),
        (["clear-state"], "clear", 8),
        ([], "run", 0),
    ),
)
def test_main_dispatches_primary_commands(monkeypatch, argv, target, status):
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

    assert dldd_cli.main(argv) == status
    assert calls == [
        ("validate", "rules.yaml")
        if target == "validate"
        else (target, False if target == "clear" else None)
    ]
