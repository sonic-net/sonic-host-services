from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from dldd import cli as dldd_cli
from dldd import preflight as dldd_preflight
from dldd.adapters import adapter_map
from dldd.dse import (
    DSEBinding,
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSEHook,
    DSERegistry,
    DSESourceHandle,
    ResolvedEvaluation,
)
from dldd.hooks import VendorHookRegistry
from dldd.models import ValueConfig
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.validation import ExactCompatibilityMatcher


FIXTURES = Path(__file__).parent / "fixtures"


class QualificationSources(object):
    def __init__(self):
        self.direct_reads = []
        self.dse_expansions = 0
        self.dse_reads = []
        self.comparator_reads = []
        self.dse_values = {
            "DSE_SENSOR0": "8",
            "DSE_SENSOR1": "12",
        }

    def read_direct(self, database, table, key):
        self.direct_reads.append((database, table, key))
        return {"value": "12"}


class QualificationDSEHook(DSEHook):
    def __init__(self, sources):
        self.sources = sources

    def resolve_source(self, reference, context):
        def expand(unused_context):
            self.sources.dse_expansions += 1
            return DSEExpansionResult(
                tuple(
                    DSEBinding(
                        instance=instance,
                        source_id="DSE_SENSOR|{}".format(instance),
                        value_configs=ValueConfig(type="float"),
                    )
                    for instance in sorted(self.sources.dse_values)
                )
            )

        def get_value(invocation):
            self.sources.dse_reads.append(invocation.binding.instance)
            return self.sources.dse_values[invocation.binding.instance]

        return DSESourceHandle(reference, expand, get_value)

    def resolve_evaluation(self, reference, context):
        def get_comparator(invocation):
            self.sources.comparator_reads.append(invocation.binding.instance)
            return ResolvedEvaluation(
                expected_value=10.0,
                operator=">",
                value_configs=ValueConfig(type="float"),
            )

        return DSEEvaluationHandle(reference, get_comparator)


def _direct_rule_document():
    with (FIXTURES / "valid-redis-rule.json").open() as stream:
        document = json.load(stream)
    signature = document["signatures"][0]["signature"]
    metadata = signature["metadata"]
    metadata.update(
        name="DIRECT_QUALIFICATION_RULE",
        id=9910001,
        product_ids=["TEST-PRODUCT"],
        sw_versions=["TEST-SOFTWARE"],
        component="TEST_SENSOR",
        severity="WARNING",
    )
    signature["conditions"]["logic"] = "1"
    signature["conditions"]["logic_lookback_time"] = 0
    event = signature["conditions"]["events"][0]["event"]
    event.update(
        type="redis",
        path={
            "database": "STATE_DB",
            "table": "DLDD_TEST_SENSOR",
            "key": "DLDD_TEST_SENSOR|DIRECT0",
            "path": "value",
        },
        evaluation={
            "type": "comparison",
            "operator": ">",
            "value": 10.0,
            "value_configs": {"type": "float", "unit": "N/A"},
        },
        match_count=1,
        match_period=0,
    )
    repair = signature["actions"]["repair_actions"]
    repair.pop("local_actions", None)
    signature["actions"].pop("log_collection", None)
    return document


def _direct_and_dse_document():
    document = _direct_rule_document()
    dse_wrapper = deepcopy(document["signatures"][0])
    dse = dse_wrapper["signature"]
    dse["metadata"].update(name="DSE_QUALIFICATION_RULE", id=9910002)
    event = dse["conditions"]["events"][0]["event"]
    event.update(
        type="dse",
        path="{sensor*}:{get_value()}",
        evaluation={
            "type": "dse",
            "value": "{sensor*}:{get_high_threshold()}",
        },
    )
    document["signatures"].append(dse_wrapper)
    return document


def _installed_qualification_environment(monkeypatch, tmp_path):
    sources = QualificationSources()
    identity = PlatformIdentity(
        "test-platform", "TEST-PRODUCT", "TEST-SOFTWARE"
    )
    extensions = PlatformExtensions(
        identity,
        DSERegistry(hook=QualificationDSEHook(sources)),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: identity,
    )
    monkeypatch.setattr(
        dldd_cli, "load_extensions", lambda *unused_args: extensions
    )
    monkeypatch.setattr(
        dldd_preflight,
        "adapter_map",
        lambda hooks: adapter_map(
            hooks=hooks,
            redis_reader=sources.read_direct,
        ),
    )
    platform_dir = tmp_path / "platform"
    platform_dir.mkdir()
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        yaml.safe_dump(_direct_and_dse_document(), sort_keys=False),
        encoding="utf-8",
    )
    return sources, platform_dir, rules_path


def _run_cli(capsys, rules_path, platform_dir, mode):
    status = dldd_cli.main(
        [
            "validate-rules",
            "--file",
            str(rules_path),
            "--platform-dir",
            str(platform_dir),
            "--mode",
            mode,
            "--json",
        ]
    )
    return status, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("mode", ("dse-resolve", "activation-dry-run"))
def test_real_yaml_non_io_modes_do_not_collect_sources(
    monkeypatch, tmp_path, capsys, mode
):
    sources, platform_dir, rules_path = (
        _installed_qualification_environment(monkeypatch, tmp_path)
    )

    status, payload = _run_cli(capsys, rules_path, platform_dir, mode)

    assert status == 0
    assert payload["file_level_result"] == "PASSED"
    assert payload["rule_level_result"] == "PASSED"
    assert payload["rules_parsed_successfully"] == 2
    assert sources.direct_reads == []
    assert sources.dse_expansions == 0
    assert sources.dse_reads == []
    assert sources.comparator_reads == []


def test_real_yaml_e2e_executes_direct_and_every_dse_instance_without_mutation(
    monkeypatch, tmp_path, capsys
):
    sources, platform_dir, rules_path = (
        _installed_qualification_environment(monkeypatch, tmp_path)
    )
    original_rules = rules_path.read_bytes()
    original_paths = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    status, payload = _run_cli(
        capsys, rules_path, platform_dir, "e2e-execute"
    )

    assert status == 0
    assert payload["qualification_result"] == "PASSED"
    assert sources.direct_reads == [
        ("STATE_DB", "DLDD_TEST_SENSOR", "DLDD_TEST_SENSOR|DIRECT0")
    ]
    assert sources.dse_expansions == 1
    assert sources.dse_reads == ["DSE_SENSOR0", "DSE_SENSOR1"]
    assert sources.comparator_reads == ["DSE_SENSOR0", "DSE_SENSOR1"]
    execution = {
        (item["rule"], item["component"]): item["state"]
        for item in payload["probe_results"]
        if item["stage"] == "execution"
    }
    assert execution == {
        ("DIRECT_QUALIFICATION_RULE", "TEST_SENSOR"): "MATCH",
        ("DSE_QUALIFICATION_RULE", "DSE_SENSOR0"): "NO_MATCH",
        ("DSE_QUALIFICATION_RULE", "DSE_SENSOR1"): "MATCH",
    }
    assert {
        (item["rule"], item["component"], item["state"])
        for item in payload["rule_results"]
    } == {
        ("DIRECT_QUALIFICATION_RULE", "TEST_SENSOR", "MATCH"),
        ("DSE_QUALIFICATION_RULE", "DSE_SENSOR0", "NO_MATCH"),
        ("DSE_QUALIFICATION_RULE", "DSE_SENSOR1", "MATCH"),
    }
    assert rules_path.read_bytes() == original_rules
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == (
        original_paths
    )


def test_real_yaml_static_cli_localizes_one_broken_rule(
    monkeypatch, tmp_path, capsys
):
    document = _direct_and_dse_document()
    document["signatures"][1]["signature"]["metadata"].pop("severity")
    rules_path = tmp_path / "mixed.yaml"
    rules_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        dldd_cli,
        "load_extensions",
        lambda *unused_args: pytest.fail(
            "static-schema unexpectedly loaded platform extensions"
        ),
    )

    status, payload = _run_cli(
        capsys, rules_path, tmp_path, "static-schema"
    )

    assert status == 1
    assert payload["file_level_result"] == "FAILED"
    assert payload["rule_level_result"] == "FAILED"
    assert payload["rules_parsed_successfully"] == 0
    assert payload["rules_failed_validation"] == 1
    issue = payload["broken_rules"][0]["issues"][0]
    assert issue["code"] == "missing_field"
    assert issue["path"].endswith("metadata.severity")
