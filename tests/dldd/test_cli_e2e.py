from __future__ import absolute_import

import json
from types import SimpleNamespace

from dldd import cli as dldd_cli
from dldd.qualification import qualify_e2e


def test_e2e_reports_a_missing_direct_adapter_without_running_remediation():
    item = SimpleNamespace(
        rule_id=1000001,
        rule_name="DIRECT_QUALIFICATION",
        event_id=1,
        component_name="SENSOR0",
        correlation_key="1000001:1:SENSOR0:redis",
        source_id="SENSOR|0",
        source_type="redis",
    )
    bundle = SimpleNamespace(
        work_items={item.correlation_key: item},
        templates={},
        signatures={},
    )

    result = qualify_e2e(bundle, {})

    assert result.failed is True
    assert result.event_results[0]["state"] == "EXECUTION_ERROR"
    assert "no adapter is registered for source type 'redis'" in (
        result.event_results[0]["error"]
    )
    assert result.rule_results == (
        {
            "rule": "DIRECT_QUALIFICATION",
            "rule_id": 1000001,
            "component": "SENSOR0",
            "stage": "rule_logic",
            "state": "UNQUALIFIED",
            "reason": "no adapter is registered for source type 'redis'",
        },
    )


def test_e2e_empty_dse_inventory_is_unqualified(monkeypatch, capsys):
    item = SimpleNamespace(
        rule_id=1000002,
        rule_name="DSE_QUALIFICATION",
        rule_version="1.0.0",
        event_id=1,
        component_name="DSE_SENSOR",
        correlation_key="template:1000002:1:sensor",
        source_type="dse",
    )
    template = SimpleNamespace(item=item, static_work_keys=())
    bundle = SimpleNamespace(
        work_items={},
        templates={"template": template},
        signatures={},
    )

    class EmptyDSEAdapter(object):
        def expand(self, unused_template):
            return SimpleNamespace(bindings=())

    validation = SimpleNamespace(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(
            SimpleNamespace(
                signature=SimpleNamespace(
                    metadata=SimpleNamespace(id=1000002)
                )
            ),
        ),
        broken_rules=(),
        file_errors=(),
        file_valid=True,
        activation_valid=True,
    )
    extensions = SimpleNamespace(
        dse_registry=SimpleNamespace(),
        compatibility_matcher=SimpleNamespace(),
    )
    preflight = SimpleNamespace(
        adapters={"dse": EmptyDSEAdapter()},
        plan=bundle,
        invalid_rule_ids=frozenset(),
        failures=(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: SimpleNamespace(
            platform="test", product_id="product", software_version="software"
        ),
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

    status = dldd_cli.validate_rules(
        SimpleNamespace(
            mode="e2e-execute",
            platform_dir=None,
            dse=None,
            file="rules.yaml",
            json=True,
            verbose=False,
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["qualification_result"] == "FAILED"
    assert payload["probe_results"][0]["state"] == "UNQUALIFIED"
    assert payload["rule_results"][0]["state"] == "UNQUALIFIED"
