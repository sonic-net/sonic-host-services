from __future__ import absolute_import

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd import qualification as dldd_qualification
from dldd.adapters import DSEAdapter
from dldd.correlation import SignatureExecution
from dldd.dse import (
    DSEBinding,
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSERegistry,
    DSESourceHandle,
    ResolvedEvaluation,
    parse_reference,
)
from dldd.hooks import VendorHookRegistry
from dldd.logic import parse_logic
from dldd.models import ValueConfig
from dldd.platform import PlatformIdentity
from dldd.qualification import qualify_e2e
from dldd.runtime import (
    DSEWorkTemplate,
    EvaluationResult,
    EvaluationResultType,
    MonitorWorkItem,
)


RULE_ID = 1000001
DIRECT_RULE_ID = 1000002


def _signature(
    *,
    rule_id=RULE_ID,
    rule_name="DSE_CLI_TEST",
    event_id=1,
    match_count=1,
    match_period=0,
):
    metadata = SimpleNamespace(
        id=rule_id,
        name=rule_name,
        version="1.0.0",
    )
    conditions = SimpleNamespace(
        events=(
            SimpleNamespace(
                id=event_id,
                match_count=match_count,
                match_period=match_period,
            ),
        ),
        logic_tree=parse_logic(str(event_id)),
        logic_lookback_time=0,
    )
    return SimpleNamespace(
        schema_version="0.0.1",
        metadata=metadata,
        conditions=conditions,
    )


def _work_item(
    correlation_key,
    source_type,
    *,
    rule_id=RULE_ID,
    rule_name="DSE_CLI_TEST",
    event_id=1,
    source_handle=None,
    evaluation_handle=None,
):
    return MonitorWorkItem(
        rule_id=rule_id,
        rule_name=rule_name,
        rule_version="1.0.0",
        schema_version="0.0.1",
        severity="WARNING",
        priority=1,
        symptom="SYMPTOM_OVER_THRESHOLD",
        error_type="POWER",
        component_type="TEMPERATURE_SENSOR",
        component_name="TEMPERATURE_SENSOR",
        event_id=event_id,
        correlation_key=correlation_key,
        source_id=correlation_key,
        source_type=source_type,
        source=(
            {"database": "STATE_DB", "table": "TEST", "key": "TEST|0"}
            if source_type == "redis"
            else {"reference": "{sensor*}:{get_value()}"}
        ),
        evaluation={
            "type": "dse" if source_type == "dse" else "comparison",
            "operator": ">=",
            "value": 10.0,
            "value_configs": ValueConfig(type="float").as_payload(),
        },
        value_config=ValueConfig(type="float"),
        dse_context=SimpleNamespace(component="TEMPERATURE_SENSOR"),
        dse_source_handle=source_handle,
        dse_evaluation_handle=evaluation_handle,
    )


def _template(expand, get_value, get_comparator=None, signature=None):
    signature = signature or _signature()
    rule_id = signature.metadata.id
    rule_name = signature.metadata.name
    event_id = signature.conditions.events[0].id
    source_reference = parse_reference("{sensor*}:{get_value()}")
    evaluation_reference = parse_reference(
        "{sensor*}:{get_high_threshold()}"
    )
    source_handle = DSESourceHandle(source_reference, expand, get_value)
    evaluation_handle = DSEEvaluationHandle(
        evaluation_reference,
        get_comparator
        or (
            lambda unused_invocation: ResolvedEvaluation(
                expected_value=10.0,
                operator=">=",
                value_configs=ValueConfig(type="float"),
            )
        ),
    )
    item = _work_item(
        "template:{}:{}:{}".format(
            rule_id, event_id, source_reference.canonical
        ),
        "dse",
        rule_id=rule_id,
        rule_name=rule_name,
        event_id=event_id,
        source_handle=source_handle,
        evaluation_handle=evaluation_handle,
    )
    return DSEWorkTemplate(
        template_id="{}:{}:{}".format(
            rule_id, event_id, source_reference.canonical
        ),
        item=item,
        signature=signature,
        source_handle=source_handle,
        evaluation_handle=evaluation_handle,
    )


def _run_cli(
    monkeypatch,
    capsys,
    *,
    mode,
    work_items,
    templates,
    adapters,
    signatures=None,
):
    rule_identities = {
        (item.rule_id, item.rule_name, item.rule_version)
        for item in tuple(work_items.values())
        + tuple(template.item for template in templates.values())
    }
    materialized = tuple(
        SimpleNamespace(
            signature=SimpleNamespace(
                metadata=SimpleNamespace(
                    id=rule_id,
                    name=rule_name,
                    version=rule_version,
                )
            )
        )
        for rule_id, rule_name, rule_version in sorted(rule_identities)
    )
    validation = SimpleNamespace(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=materialized,
        broken_rules=(),
        file_errors=(),
        file_valid=True,
        activation_valid=True,
        source_lines={"$": 1, "$.signatures": 2},
    )
    extensions = SimpleNamespace(
        dse_registry=DSERegistry(),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=SimpleNamespace(),
    )
    plan = SimpleNamespace(
        work_items=work_items,
        templates=templates,
        signatures=signatures or {},
    )
    preflight = SimpleNamespace(
        plan=plan,
        adapters=adapters,
        failures=(),
        invalid_rule_ids=frozenset(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: PlatformIdentity("test", "product", "software"),
    )
    monkeypatch.setattr(
        dldd_cli, "load_extensions", lambda *unused_args: extensions
    )
    monkeypatch.setattr(
        dldd_cli, "load_rules", lambda *unused_args, **unused_kwargs: validation
    )
    monkeypatch.setattr(
        dldd_cli,
        "preflight_activation",
        lambda *unused_args, **unused_kwargs: preflight,
    )
    args = SimpleNamespace(
        mode=mode,
        platform_dir=None,
        dse=None,
        file="rules.yaml",
        json=True,
        verbose=False,
    )

    status = dldd_cli.validate_rules(args)
    return status, json.loads(capsys.readouterr().out)


def test_e2e_executes_direct_work_and_every_expanded_dse_child(
    monkeypatch, capsys
):
    calls = {"expand": 0, "source": [], "comparator": [], "direct": 0}
    values = {"SENSOR0": "12.5", "SENSOR1": "2.5"}

    def expand(unused_context):
        calls["expand"] += 1
        return DSEExpansionResult(
            tuple(
                DSEBinding(
                    instance=name,
                    source_id="SENSOR_INFO|{}".format(name),
                    value_configs=ValueConfig(type="float", unit="C"),
                )
                for name in sorted(values)
            )
        )

    def get_value(invocation):
        calls["source"].append(invocation.binding.instance)
        return values[invocation.binding.instance]

    def get_comparator(invocation):
        calls["comparator"].append(invocation.binding.instance)
        return ResolvedEvaluation(
            expected_value=10.0,
            operator=">=",
            value_configs=ValueConfig(type="float", unit="C"),
        )

    template = _template(expand, get_value, get_comparator)
    direct_signature = _signature(
        rule_id=DIRECT_RULE_ID,
        rule_name="DIRECT_CLI_TEST",
    )
    direct = _work_item(
        "direct:redis",
        "redis",
        rule_id=DIRECT_RULE_ID,
        rule_name="DIRECT_CLI_TEST",
    )
    signatures = {
        (DIRECT_RULE_ID, direct.component_name): SignatureExecution(
            direct_signature,
            direct.component_name,
            {direct.event_id: (direct.correlation_key,)},
            "e2e-qualification",
        )
    }

    class DirectAdapter(object):
        def collect(self, item):
            assert item is direct
            calls["direct"] += 1
            return EvaluationResult(EvaluationResultType.NO_MATCH)

    status, payload = _run_cli(
        monkeypatch,
        capsys,
        mode="e2e-execute",
        work_items={direct.correlation_key: direct},
        templates={template.template_id: template},
        adapters={"redis": DirectAdapter(), "dse": DSEAdapter()},
        signatures=signatures,
    )

    assert status == 0
    assert payload["qualification_result"] == "PASSED"
    assert calls == {
        "expand": 1,
        "source": ["SENSOR0", "SENSOR1"],
        "comparator": ["SENSOR0", "SENSOR1"],
        "direct": 1,
    }
    assert [item["state"] for item in payload["probe_results"]] == [
        "EXPANDED",
        "NO_MATCH",
        "MATCH",
        "NO_MATCH",
    ]
    assert "SENSOR0" in payload["probe_results"][2]["correlation_key"]
    assert "SENSOR1" in payload["probe_results"][3]["correlation_key"]
    assert [item["state"] for item in payload["rule_results"]] == [
        "MATCH",
        "NO_MATCH",
        "NO_MATCH",
    ]
    assert [item["component"] for item in payload["rule_results"]] == [
        "SENSOR0",
        "SENSOR1",
        "TEMPERATURE_SENSOR",
    ]
    assert [item["rule"] for item in payload["rule_results"]] == [
        "DSE_CLI_TEST",
        "DSE_CLI_TEST",
        "DIRECT_CLI_TEST",
    ]


def test_dse_expansion_runtime_and_non_e2e_boundary_contract(
    monkeypatch, capsys
):
    def expand(unused_context):
        raise RuntimeError("inventory backend is unavailable")

    template = _template(expand, lambda unused_invocation: 1)

    status, payload = _run_cli(
        monkeypatch,
        capsys,
        mode="e2e-execute",
        work_items={},
        templates={template.template_id: template},
        adapters={"dse": DSEAdapter()},
    )

    assert status == 1
    assert payload["qualification_result"] == "FAILED"
    expansion = payload["probe_results"][0]
    assert expansion["stage"] == "expansion"
    assert expansion["state"] == "EXPANSION_ERROR"
    assert expansion["error"] == "inventory backend is unavailable"
    assert payload["rules_parsed_successfully"] == 1
    assert payload["rules_failed_validation"] == 0
    assert payload["rule_results"] == [
        {
            "rule": "DSE_CLI_TEST",
            "rule_id": RULE_ID,
            "component": None,
            "stage": "rule_logic",
            "state": "UNQUALIFIED",
            "reason": "inventory backend is unavailable",
        }
    ]


    for failure_stage, expected_state in (
        ("collection", "COLLECTION_ERROR"),
        ("comparator", "EVALUATION_ERROR"),
    ):
        binding = DSEBinding(
            instance="SENSOR0", source_id="SENSOR_INFO|SENSOR0"
        )

        def fail_source(unused_invocation):
            raise RuntimeError("sensor read failed")

        def fail_comparator(unused_invocation):
            raise RuntimeError("threshold read failed")

        get_value = (
            fail_source
            if failure_stage == "collection"
            else lambda unused_invocation: 12.0
        )
        get_comparator = (
            fail_comparator if failure_stage == "comparator" else None
        )
        template = _template(
            lambda unused_context: DSEExpansionResult((binding,)),
            get_value,
            get_comparator,
        )
        status, payload = _run_cli(
            monkeypatch,
            capsys,
            mode="e2e-execute",
            work_items={},
            templates={template.template_id: template},
            adapters={"dse": DSEAdapter()},
        )
        assert status == 1
        assert payload["qualification_result"] == "FAILED"
        assert payload["probe_results"][0]["state"] == "EXPANDED"
        assert payload["probe_results"][1]["state"] == expected_state
        assert payload["rules_parsed_successfully"] == 1
        assert payload["rules_failed_validation"] == 0
        assert payload["rule_results"][0]["state"] == "UNQUALIFIED"

    for mode in ("activation-dry-run", "hardware-probe"):

        def unexpected_expand(unused_context):
            pytest.fail("non-e2e validation expanded DSE inventory")

        template = _template(unexpected_expand, lambda unused_invocation: 1)
        status, payload = _run_cli(
            monkeypatch,
            capsys,
            mode=mode,
            work_items={},
            templates={template.template_id: template},
            adapters={"dse": DSEAdapter()},
        )
        assert status == 0
        assert payload["probe_results"] == []

    template = _template(
        lambda unused_context: DSEExpansionResult(()),
        lambda unused_invocation: 1,
    )

    status, payload = _run_cli(
        monkeypatch,
        capsys,
        mode="e2e-execute",
        work_items={},
        templates={template.template_id: template},
        adapters={"dse": DSEAdapter()},
    )

    assert status == 1
    assert payload["qualification_result"] == "FAILED"
    assert payload["probe_results"] == [
        {
            "rule": "DSE_CLI_TEST",
            "rule_id": RULE_ID,
            "event_id": 1,
            "component": None,
            "correlation_key": template.item.correlation_key,
            "stage": "expansion",
            "state": "UNQUALIFIED",
            "instance_count": 0,
            "reason": "DSE expansion discovered no instances",
        }
    ]
    assert payload["rule_results"][0]["state"] == "UNQUALIFIED"


def test_e2e_collects_a_shared_expanded_common_predicate_once():
    binding = DSEBinding(instance="SENSOR0", source_id="SENSOR_INFO|SENSOR0")

    def expansion(unused_context):
        return DSEExpansionResult((binding,))

    first = _template(expansion, lambda unused_invocation: 12.0)
    common = _work_item("common:source", "redis")
    first = replace(first, common_items=(common,))

    second_reference = parse_reference("{sensor*}:{get_backup_value()}")
    second_handle = DSESourceHandle(
        second_reference,
        expansion,
        lambda unused_invocation: 12.0,
    )
    second_base = replace(
        first.item,
        correlation_key="template:{}:1:{}".format(
            RULE_ID, second_reference.canonical
        ),
        source_id=second_reference.canonical,
        source={"reference": second_reference.canonical},
        dse_source_handle=second_handle,
    )
    second = replace(
        first,
        template_id="{}:1:{}".format(RULE_ID, second_reference.canonical),
        item=second_base,
        source_handle=second_handle,
    )
    calls = {"common": 0}

    class CommonAdapter(object):
        def collect(self, unused_item):
            calls["common"] += 1
            return EvaluationResult(EvaluationResultType.NO_MATCH)

    bundle = SimpleNamespace(
        work_items={},
        templates={first.template_id: first, second.template_id: second},
        signatures={},
    )

    result = qualify_e2e(
        bundle,
        {"dse": DSEAdapter(), "redis": CommonAdapter()},
    )

    assert result.failed is False
    assert calls["common"] == 1


def test_direct_e2e_adapter_preflight_and_correlation_contract(monkeypatch):
    for adapter, error in (
        (None, "no adapter is registered for source type 'redis'"),
        ("raises", "collector implementation failed"),
        ("invalid", "adapter collect() must return EvaluationResult"),
    ):
        direct = _work_item("direct:redis", "redis")
        signature = _signature()
        execution = SignatureExecution(
            signature,
            direct.component_name,
            {direct.event_id: (direct.correlation_key,)},
            "e2e-qualification",
        )

        class RaisingAdapter(object):
            def collect(self, unused_item):
                raise RuntimeError("collector implementation failed")

        class InvalidAdapter(object):
            def collect(self, unused_item):
                return object()

        resolved_adapter = {
            None: None,
            "raises": RaisingAdapter(),
            "invalid": InvalidAdapter(),
        }[adapter]
        adapters = (
            {} if resolved_adapter is None else {"redis": resolved_adapter}
        )
        bundle = SimpleNamespace(
            work_items={direct.correlation_key: direct},
            templates={},
            signatures={(RULE_ID, direct.component_name): execution},
        )
        result = qualify_e2e(bundle, adapters)
        assert result.failed is True
        assert result.event_results[0]["state"] == "EXECUTION_ERROR"
        assert error in result.event_results[0]["error"]
        assert result.rule_results[0]["state"] == "UNQUALIFIED"

    direct = _work_item("direct:redis", "redis")

    class MatchingAdapter(object):
        def collect(self, unused_item):
            return EvaluationResult(EvaluationResultType.MATCH)

    bundle = SimpleNamespace(
        work_items={direct.correlation_key: direct},
        templates={},
        signatures={},
    )

    result = qualify_e2e(bundle, {"redis": MatchingAdapter()})

    assert result.failed is True
    assert result.event_results[0]["state"] == "MATCH"
    assert result.rule_results[0] == {
        "rule": "DSE_CLI_TEST",
        "rule_id": RULE_ID,
        "component": "TEMPERATURE_SENSOR",
        "stage": "rule_logic",
        "state": "UNQUALIFIED",
        "reason": "rule correlation did not accept the event",
    }

    direct = _work_item("direct:redis", "redis")
    signature = _signature(match_count=2, match_period=60)
    execution = SignatureExecution(
        signature,
        direct.component_name,
        {direct.event_id: (direct.correlation_key,)},
        "e2e-qualification",
    )

    class MatchingAdapter(object):
        def collect(self, unused_item):
            return EvaluationResult(EvaluationResultType.MATCH)

    bundle = SimpleNamespace(
        work_items={direct.correlation_key: direct},
        templates={},
        signatures={(RULE_ID, direct.component_name): execution},
    )

    result = qualify_e2e(bundle, {"redis": MatchingAdapter()})

    assert result.failed is False
    assert result.event_results[0]["state"] == "MATCH"
    assert result.rule_results[0]["state"] == "NO_MATCH"


    def unexpected_expand(unused_context):
        pytest.fail("qualification expanded a preflight-rejected rule")

    template = _template(unexpected_expand, lambda unused_invocation: 1)
    direct = _work_item("direct:redis", "redis")
    bundle = SimpleNamespace(
        work_items={direct.correlation_key: direct},
        templates={template.template_id: template},
        signatures={},
    )

    result = qualify_e2e(bundle, {}, invalid_rule_ids={RULE_ID})

    assert result.failed is False
    assert result.event_results == ()
    assert result.rule_results == ()

    def unexpected_expand(unused_context):
        pytest.fail("qualification invoked an unavailable DSE adapter")

    template = _template(unexpected_expand, lambda unused_invocation: 1)
    bundle = SimpleNamespace(
        work_items={},
        templates={template.template_id: template},
        signatures={},
    )

    result = qualify_e2e(bundle, {})

    assert result.failed is True
    assert result.event_results[0]["state"] == "EXPANSION_ERROR"
    assert "no adapter is registered for source type 'dse'" in (
        result.event_results[0]["error"]
    )
    assert result.rule_results[0]["state"] == "UNQUALIFIED"


    direct = _work_item("direct:redis", "redis")

    class MatchingAdapter(object):
        def collect(self, unused_item):
            return EvaluationResult(EvaluationResultType.MATCH)

    class FailingCorrelation(object):
        def __init__(self, unused_signatures):
            pass

        def consume(self, unused_evidence):
            raise RuntimeError("correlation implementation failed")

    monkeypatch.setattr(
        dldd_qualification, "CorrelationEngine", FailingCorrelation
    )
    bundle = SimpleNamespace(
        work_items={direct.correlation_key: direct},
        templates={},
        signatures={},
    )

    result = qualify_e2e(bundle, {"redis": MatchingAdapter()})

    assert result.failed is True
    assert result.event_results[0]["state"] == "MATCH"
    assert result.rule_results[0] == {
        "rule": "DSE_CLI_TEST",
        "rule_id": RULE_ID,
        "component": "TEMPERATURE_SENSOR",
        "stage": "rule_logic",
        "state": "UNQUALIFIED",
        "reason": "correlation implementation failed",
    }
