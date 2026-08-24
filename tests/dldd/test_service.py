from __future__ import absolute_import

import json
from queue import Queue
from types import SimpleNamespace

import pytest

from dldd import service as dldd_service
from dldd.artifacts import (
    DEFAULT_ARTIFACT_DIRECTORY,
    ArtifactRequest,
    HealthzArtifactClient,
)
from dldd.config import ConfigDBProvider, DLDDConfig
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.models import BrokenRule, ValidationIssue, ValidationResult
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.planner import build_plans
from dldd.runtime import (
    MonitorExecutionPlan,
    MonitorWorkState,
    MonitorWorkStateRecord,
)
from dldd.service import (
    DLDDService,
    TelemetryUnavailable,
)
from dldd.validation import ExactCompatibilityMatcher, load_rules


class VendorArtifactClient(HealthzArtifactClient):
    def request(self, metadata, logs, queries):
        return ArtifactRequest("vendor-artifact", "REQUESTED", 1.0)

    def status(self, artifact_id):
        return ArtifactRequest(artifact_id, "COMPLETED", 1.0, 2.0)

    def shutdown(self, wait=True):
        return None


@pytest.mark.parametrize(
    "code,message,prefix",
    (
        ("invalid_dse_reference", "unknown DSE binding", "dse_error:"),
        ("invalid_operator", "operator cannot execute", "evaluation_error:"),
        ("missing_field", "severity is required", "schema_error:"),
        ("unknown_field", "field is not permitted", "schema_error:"),
        ("out_of_range", "value exceeds its bound", "schema_error:"),
        ("unsupported_type", "type is unsupported", "schema_error:"),
        ("materialization_failed", "platform binding failed", "validation_error:"),
    ),
)
def test_ingestion_failure_reason_uses_hld_category(code, message, prefix):
    issue = ValidationIssue("rule", code, message)

    assert dldd_service._ingestion_failure_reason((issue,)).startswith(prefix)


def test_operator_status_records_use_only_public_rule_instance_identity():
    records = (
        {"correlation_key": "work-1", "reason": "resolved from work"},
        {
            "correlation_key": "work-2",
            "rule_id": 1000002,
            "component_name": "PSU2",
        },
        {
            "correlation_key": "legacy-diagnostic",
            "rule_id": 1000003,
            "component": "FAN0",
            "rule_instance_id": "1000003@FAN0",
        },
        {
            "correlation_key": "rule:1000004",
            "rule_id": 1000004,
            "reason": "ingestion failure",
        },
        {"correlation_key": "component-only", "component": "ASIC0"},
        "opaque-diagnostic",
    )
    work_items = {
        "work-1": SimpleNamespace(
            rule_id=1000001,
            component_type="PSU",
            component_name="PSU1",
        ),
        "work-2": SimpleNamespace(
            rule_id=1000002,
            component_type="PSU",
            component_name="PSU2",
        ),
    }

    projected = dldd_service._operator_status_records(records, work_items)

    assert projected[0] == {
        "reason": "resolved from work",
        "rule_id": 1000001,
        "component_type": "PSU",
        "component_name": "PSU1",
        "rule_instance_id": "1000001@PSU1",
    }
    assert projected[1] == {
        "rule_id": 1000002,
        "component_name": "PSU2",
        "component_type": "PSU",
        "rule_instance_id": "1000002@PSU2",
    }
    assert projected[2] == {
        "rule_id": 1000003,
        "component_name": "FAN0",
        "rule_instance_id": "1000003@FAN0",
    }
    assert projected[3] == {
        "rule_id": 1000004,
        "reason": "ingestion failure",
    }
    assert projected[4] == {"component_name": "ASIC0"}
    assert projected[5] == "opaque-diagnostic"
    assert records[0]["correlation_key"] == "work-1"


def test_service_artifact_client_creation_contract(tmp_path, monkeypatch):
    identity = PlatformIdentity("test", "product", "software")
    client = VendorArtifactClient()
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return client

    extensions = PlatformExtensions(
        identity,
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
        factory,
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )

    created = service._create_artifact_client()

    assert created is client
    assert captured["identity"] is identity
    assert captured["artifact_directory"] == DEFAULT_ARTIFACT_DIRECTORY
    assert captured["query_runner"].__self__ is service

    extensions = PlatformExtensions(
        PlatformIdentity("test", "product", "software"),
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
        lambda **kwargs: object(),
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )

    with pytest.raises(TypeError, match="must return HealthzArtifactClient"):
        service._create_artifact_client()

    identity = PlatformIdentity("test", "product", "software")
    extensions = SimpleNamespace(
        identity=identity,
        dse_registry=DSERegistry(),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=ExactCompatibilityMatcher(),
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )
    sentinel = object()
    captured = {}

    def create_default(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(dldd_service, "FilesystemArtifactClient", create_default)

    assert service._create_artifact_client() is sentinel
    assert captured["directory"] == DEFAULT_ARTIFACT_DIRECTORY
    assert captured["query_runner"].__self__ is service


@pytest.mark.parametrize("failure_mode", ("exception", "invalid-result"))
def test_artifact_factory_failure_publishes_fatal_without_workers(
    tmp_path, monkeypatch, failure_mode
):
    def factory(**kwargs):
        if failure_mode == "exception":
            raise RuntimeError("vendor artifact store is unavailable")
        return object()

    extensions = PlatformExtensions(
        PlatformIdentity("test", "product", "software"),
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
        factory,
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )
    validation = SimpleNamespace(
        materialized_rules=(
            SimpleNamespace(
                signature=SimpleNamespace(metadata=SimpleNamespace(id=1000001))
            ),
        )
    )
    activation = SimpleNamespace(
        payload=validation,
        checksum="sha256:test",
        schema_version="0.0.1",
        active_file="rules.yaml",
        broken_rules=(),
    )
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        rule_name="VALID_RULE",
        rule_version="1.0.0",
        correlation_key="1000001:event:component:source",
    )

    class AcceptingAdapter(object):
        def validate(self, unused_item):
            return None

    class CapturingTelemetry(object):
        def __init__(self, *args, **kwargs):
            self.status = None

        def publish_status(self, *args, **kwargs):
            self.status = (args, kwargs)

    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service.RuleGenerationManager,
        "activate",
        lambda unused_manager: activation,
    )
    monkeypatch.setattr(dldd_service, "TelemetryPublisher", CapturingTelemetry)
    monkeypatch.setattr(
        dldd_service,
        "build_plans",
        lambda *args, **kwargs: SimpleNamespace(
            work_items={item.correlation_key: item},
            monitor_plans={},
            signatures={},
            templates={},
        ),
    )
    monkeypatch.setattr(
        service, "_adapters", lambda: {"redis": AcceptingAdapter()}
    )

    service.start()

    assert service.telemetry.status[0][0] == "BROKEN|FATAL"
    assert "artifact client initialization failed" in service.fatal_reason
    assert service.action_runner is None
    assert service.artifact_client is None
    assert service.monitors == []


def test_ingestion_broken_rule_and_external_byte_cap_contract(
    tmp_path, monkeypatch
):
    issue = ValidationIssue("rule", "missing_field", "severity is required")
    broken_rule = BrokenRule(
        rule_name="BAD_RULE",
        rule_id=1000001,
        rule_version="2.3.4",
        issues=(issue,),
    )
    validation = SimpleNamespace(
        schema_version="0.0.1",
        materialized_rules=(),
        broken_rules=(broken_rule,),
        file_errors=(),
        file_valid=True,
    )
    extensions = PlatformExtensions(
        PlatformIdentity("test", "product", "software"),
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )
    monkeypatch.setattr(dldd_service, "load_rules", lambda *args: validation)
    monkeypatch.setattr(dldd_service.time, "time", lambda: 1234.5)

    candidate = service._validate_candidate("rules.yaml", "dse.yaml")

    assert candidate.broken_rules == (
        {
            "rule": "BAD_RULE",
            "rule_id": 1000001,
            "version": "2.3.4",
            "state": "BROKEN",
            "reason": "schema_error: $: severity is required (missing_field)",
            "failure_count": 1,
            "last_attempt": 1234,
        },
    )

    broken_rules = tuple(
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
        SimpleNamespace(broken_rules=broken_rules), 1234.5
    )
    serialized = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert len(records) == 1024
    assert len(serialized) <= dldd_service.MAX_SERIALIZED_DIAGNOSTIC_BYTES
    assert len({record["rule"] for record in records}) == 1024
    assert all("\0" not in record["rule"] for record in records)


def test_service_translates_preflight_failures_into_candidate(
    tmp_path, monkeypatch
):
    extensions = PlatformExtensions(
        PlatformIdentity("test", "product", "software"),
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )
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
    assert candidate.usable_rule_count == 1
    assert candidate.payload.materialized_rules == (accepted,)
    assert candidate.broken_rules[0]["rule"] == "REJECTED"
    assert candidate.broken_rules[0]["rule_id"] == 1000002
    assert candidate.broken_rules[0]["last_attempt"] == 6789.0
    assert "unsupported source binding" in candidate.broken_rules[0]["reason"]
    assert candidate.payload.broken_rules[0].issues[0].line == 7


def _rule_status_fixture(work_state=MonitorWorkState.READY):
    result = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 41, "file": 42, "common": 43},
    )
    item = next(iter(bundle.work_items.values()))
    plan = bundle.monitor_plans["redis"]
    plan.state_by_key[item.correlation_key].state = work_state
    service = object.__new__(DLDDService)
    service.activation = SimpleNamespace(
        payload=result,
        broken_rules=(),
        checksum="sha256:test",
    )
    service.orchestrator = SimpleNamespace(
        work_items=bundle.work_items,
        broken_rules={},
        faults={},
    )
    service.monitors = [SimpleNamespace(plan=plan)]
    return service, result, bundle, item, plan


def test_rule_status_snapshot_classification_grouping_and_truncation(monkeypatch):
    service, result, bundle, item, plan = _rule_status_fixture(
        MonitorWorkState.DEGRADED
    )
    state = plan.state_by_key[item.correlation_key]
    state.last_attempt_timestamp = 1234.9
    state.last_success_timestamp = 1200.8
    state.consecutive_failure_count = 3
    broken = {
        "rule": item.rule_name,
        "rule_id": item.rule_id,
        "version": item.rule_version,
        "correlation_key": item.correlation_key,
        "state": "DEGRADED",
        "failure_count": 3,
        "last_attempt": 1234.7,
        "reason": "source unavailable",
    }
    ingestion_broken = {
        "rule": "SCHEMA_BAD",
        "rule_id": 1000002,
        "version": "1.0.0",
        "state": "BROKEN",
        "failure_count": 1,
        "last_attempt": 1000.0,
        "reason": "schema_error: field is required",
    }
    service.activation = SimpleNamespace(
        payload=result,
        broken_rules=(ingestion_broken,),
        checksum="sha256:test",
    )
    service.orchestrator = SimpleNamespace(
        work_items=bundle.work_items,
        broken_rules={item.correlation_key: broken},
        faults={
            (item.rule_id, item.component_name): SimpleNamespace(
                rule_id=item.rule_id,
                component_name=item.component_name,
                status="ACTIVE",
            )
        },
    )
    service.monitors = [SimpleNamespace(plan=plan)]

    rows, truncated = service._rule_status_snapshot()

    assert not truncated
    assert len(rows) == 2
    active = rows[0]
    assert active["rule_id"] == item.rule_id
    assert active["health"] == "DEGRADED"
    assert active["work_items_healthy"] == 0
    assert active["work_items_total"] == 1
    assert active["active_faults"] == 1
    assert active["last_attempt"] == 1234
    assert active["last_success"] == 1200
    assert active["failure_count"] == 3
    assert active["reason"] == "source unavailable"
    assert active["work_items"][0]["state"] == "DEGRADED"
    assert active["work_items"][0]["sampling_interval"] == 41
    assert active["work_items"][0]["interval_source"] == (
        "monitor_default"
    )
    assert not active["work_items"][0]["async"]
    assert active["work_items"][0]["active_fault"]
    assert active["work_items"][0]["rule_instance_id"] == "{}@{}".format(
        item.rule_id, item.component_name
    )
    assert active["work_items"][0]["component_name"] == item.component_name
    assert "correlation_key" not in active["work_items"][0]
    assert rows[1]["health"] == "BROKEN"
    assert rows[1]["work_items_total"] == 0

    for work_state, expected_health in (
        (MonitorWorkState.READY, "OK"),
        (MonitorWorkState.COLLECTING, "OK"),
        (MonitorWorkState.SUSPENDED, "SUSPENDED"),
        (MonitorWorkState.BROKEN, "BROKEN"),
        (MonitorWorkState.DEGRADED, "DEGRADED"),
    ):
        classified, _, _, _, _ = _rule_status_fixture(work_state)
        rows, truncated = classified._rule_status_snapshot()
        assert not truncated
        assert rows[0]["health"] == expected_health

    failures = (
        {
            "rule": "BAD_RULE",
            "rule_id": 1000002,
            "version": "1.0.0",
            "failure_count": 1,
            "last_attempt": 1000.0,
            "reason": "first problem",
        },
        {
            "rule": "BAD_RULE",
            "rule_id": 1000002,
            "version": "1.0.0",
            "failure_count": 2,
            "last_attempt": 1001.0,
            "reason": "second problem",
        },
    )
    grouped = object.__new__(DLDDService)
    grouped.activation = SimpleNamespace(
        payload=SimpleNamespace(materialized_rules=()),
        broken_rules=failures,
        checksum="sha256:test",
    )
    grouped.orchestrator = None
    grouped.monitors = []

    rows, truncated = grouped._rule_status_snapshot()

    assert not truncated
    assert len(rows) == 1
    assert rows[0]["rule"] == "BAD_RULE"
    assert rows[0]["health"] == "BROKEN"
    assert rows[0]["failure_count"] == 2
    assert rows[0]["last_attempt"] == 1001.0
    assert rows[0]["reason"] == "first problem (+1 more)"

    omitted, _, _, _, _ = _rule_status_fixture()
    monkeypatch.setattr("dldd.rule_status.MAX_DETAILS_PER_RULE", 0)

    rows, truncated = omitted._rule_status_snapshot()

    assert truncated
    assert rows[0]["work_items"] == []
    assert rows[0]["work_items_omitted"] == 1


def test_rule_status_publication_contains_snapshot_exceptions(caplog):
    published = []
    service = object.__new__(DLDDService)
    service.activation = SimpleNamespace(checksum="sha256:test")
    service.telemetry = SimpleNamespace(
        publish_rule_status=lambda *args, **kwargs: published.append(
            (args, kwargs)
        )
    )
    service._rule_status_snapshot = lambda: (_ for _ in ()).throw(
        RuntimeError("snapshot failed")
    )

    published_ok = service._publish_rule_status()

    assert not published_ok
    assert not published
    assert "unable to build DLDD rule status snapshot" in caplog.text


def test_run_fails_after_three_consecutive_status_write_failures(monkeypatch):
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

    with pytest.raises(
        TelemetryUnavailable,
        match="failed 3 consecutive times",
    ):
        service.run()

    assert shutdown_modes == [False]


def test_inflight_status_contract(caplog):
    transient = MonitorWorkStateRecord(
        state=MonitorWorkState.IN_FLIGHT,
        last_enqueue_timestamp=100.0,
    )
    expanded_held = MonitorWorkStateRecord(
        state=MonitorWorkState.HELD_BY_PRIMARY,
        last_enqueue_timestamp=101.0,
    )
    expanded_recheck = MonitorWorkStateRecord(
        state=MonitorWorkState.RECHECK_REQUESTED,
        last_enqueue_timestamp=102.0,
    )
    static_items = {
        "transient-key": SimpleNamespace(
            rule_name="TRANSIENT_RULE",
            rule_id=1000001,
            event_id=1,
            component_type="PSU",
            component_name="PSU0",
        ),
    }
    expanded_item = SimpleNamespace(
        correlation_key="expanded-held-key",
        rule_name="EXPANDED_HELD_RULE",
        rule_id=1000002,
        event_id=2,
        component_type="PSU",
        component_name="PSU1",
    )
    expanded_recheck_item = SimpleNamespace(
        correlation_key="expanded-recheck-key",
        rule_name="EXPANDED_RECHECK_RULE",
        rule_id=1000003,
        event_id=3,
        component_type="PSU",
        component_name="PSU2",
    )
    expanded_contributor = SimpleNamespace(
        correlation_key="expanded-held-key-2",
        rule_name="EXPANDED_HELD_RULE",
        rule_id=1000002,
        event_id=4,
        component_type="PSU",
        component_name="PSU1",
    )
    plan = MonitorExecutionPlan(
        monitor_id="redis",
        monitor_type="redis",
        polling_interval=1,
        plan_generation="sha256:test",
        items_by_key=static_items,
        state_by_key={"transient-key": transient},
        control_queue=Queue(),
    )
    plan.add_expanded_item(expanded_item)
    plan.state_by_key["expanded-held-key"] = expanded_held
    plan.add_expanded_item(expanded_contributor)
    plan.state_by_key["expanded-held-key-2"] = expanded_held
    plan.add_expanded_item(expanded_recheck_item)
    plan.state_by_key["expanded-recheck-key"] = expanded_recheck
    service = object.__new__(DLDDService)
    service.monitors = [SimpleNamespace(plan=plan)]
    service.orchestrator = SimpleNamespace(pending={})

    statuses = service._inflight_status()

    assert statuses == (
        {
            "rule_instance_id": "1000002@PSU1",
            "rule": "EXPANDED_HELD_RULE",
            "rule_id": 1000002,
            "event_id": 2,
            "component_type": "PSU",
            "component_name": "PSU1",
            "state": "HELD_BY_PRIMARY",
            "reason": "primary_owned",
            "since": 101.0,
            "hold_deadline": None,
            "owning_monitor": "redis",
        },
        {
            "rule_instance_id": "1000002@PSU1",
            "rule": "EXPANDED_HELD_RULE",
            "rule_id": 1000002,
            "event_id": 4,
            "component_type": "PSU",
            "component_name": "PSU1",
            "state": "HELD_BY_PRIMARY",
            "reason": "primary_owned",
            "since": 101.0,
            "hold_deadline": None,
            "owning_monitor": "redis",
        },
        {
            "rule_instance_id": "1000003@PSU2",
            "rule": "EXPANDED_RECHECK_RULE",
            "rule_id": 1000003,
            "event_id": 3,
            "component_type": "PSU",
            "component_name": "PSU2",
            "state": "RECHECK_REQUESTED",
            "reason": "primary_owned",
            "since": 102.0,
            "hold_deadline": None,
            "owning_monitor": "redis",
        },
    )

    plan = MonitorExecutionPlan(
        monitor_id="redis",
        monitor_type="redis",
        polling_interval=1,
        plan_generation="sha256:test",
        items_by_key={},
        state_by_key={
            "missing-key": MonitorWorkStateRecord(
                state=MonitorWorkState.RECHECK_REQUESTED,
                last_enqueue_timestamp=101.0,
            )
        },
        control_queue=Queue(),
    )
    service = object.__new__(DLDDService)
    service.monitors = [SimpleNamespace(plan=plan)]
    service.orchestrator = SimpleNamespace(pending={})

    statuses = service._inflight_status()

    assert statuses == ()
    assert (
        "unable to publish in-flight status for missing work item missing-key"
        in caplog.text
    )


def test_start_does_not_repeat_candidate_adapter_preflight(
    tmp_path, monkeypatch
):
    extensions = PlatformExtensions(
        PlatformIdentity("test", "product", "software"),
        DSERegistry(),
        VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )
    service = DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=extensions,
    )
    materialized_rule = SimpleNamespace(
        signature=SimpleNamespace(metadata=SimpleNamespace(id=1000001))
    )
    validation = SimpleNamespace(materialized_rules=(materialized_rule,))
    activation = SimpleNamespace(
        payload=validation,
        checksum="sha256:test",
        schema_version="0.0.1",
        active_file="rules.yaml",
        broken_rules=(),
    )
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        rule_name="BAD_ADAPTER",
        rule_version="4.5.6",
        correlation_key="1000001:event:component:source",
    )

    validation_calls = []

    class SecondCallBugAdapter(object):
        def validate(self, unused_item):
            validation_calls.append(unused_item)
            raise RuntimeError("adapter must not be validated twice")

    class CapturingTelemetry(object):
        def __init__(self, *args, **kwargs):
            self.status = None

        def publish_status(self, *args, **kwargs):
            self.status = (args, kwargs)

    def fake_build_plans(unused_rules, *args):
        return SimpleNamespace(
            work_items={item.correlation_key: item},
            monitor_plans={},
            signatures={},
            templates={},
        )

    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service.RuleGenerationManager,
        "activate",
        lambda unused_manager: activation,
    )
    monkeypatch.setattr(dldd_service, "TelemetryPublisher", CapturingTelemetry)
    monkeypatch.setattr(dldd_service, "build_plans", fake_build_plans)
    monkeypatch.setattr(
        service, "_adapters", lambda: {"redis": SecondCallBugAdapter()}
    )
    monkeypatch.setattr(
        service,
        "_create_artifact_client",
        lambda: (_ for _ in ()).throw(RuntimeError("stop after preflight")),
    )

    service.start()

    assert validation_calls == []
    assert service.fatal_reason == (
        "artifact client initialization failed: stop after preflight"
    )


def test_dynamic_monitor_config_contract(tmp_path):
    service = object.__new__(DLDDService)
    service.paths = SimpleNamespace(defaults=str(tmp_path / "missing.yaml"))
    service.config = DLDDConfig()
    service.telemetry = SimpleNamespace(config=None)
    service.orchestrator = SimpleNamespace(config=None)
    service.orchestrator.queue_config_update = (
        lambda updated: setattr(service.orchestrator, "config", updated)
    )

    class ConfigurableMonitor(object):
        def __init__(self, monitor_type):
            self.plan = SimpleNamespace(
                monitor_type=monitor_type, polling_interval=60
            )
            self.fault_evidence_ack_timeout = 120
            self.source_recovery_samples = 1

        def update_polling_intervals(self, intervals):
            self.plan.polling_interval = intervals[self.plan.monitor_type]

    service.monitors = [
        ConfigurableMonitor(monitor_type)
        for monitor_type in ("redis", "file", "common")
    ]

    service._apply_config(
        {
            "redis_monitor_polling_interval": "7",
            "file_monitor_polling_interval": "8",
            "common_monitor_polling_interval": "9",
            "fault_evidence_ack_timeout": "10",
            "source_recovery_samples": "2",
        }
    )

    assert service.config.redis_monitor_polling_interval == 7
    assert service.telemetry.config is service.config
    assert service.orchestrator.config is service.config
    assert [monitor.plan.polling_interval for monitor in service.monitors] == [7, 8, 9]
    assert all(
        monitor.fault_evidence_ack_timeout == 10
        and monitor.source_recovery_samples == 2
        for monitor in service.monitors
    )


    service = object.__new__(DLDDService)
    service.paths = SimpleNamespace(defaults=str(tmp_path / "missing.yaml"))
    service.config = DLDDConfig()
    service.telemetry = None
    service.orchestrator = None
    updates = []

    class Monitor(object):
        def __init__(self, monitor_type, plan=None, replace_self=False):
            self.plan = plan or SimpleNamespace(
                monitor_type=monitor_type, polling_interval=60
            )
            self.fault_evidence_ack_timeout = 120
            self.source_recovery_samples = 1
            self.replace_self = replace_self

        def update_polling_intervals(self, intervals):
            updates.append(self.plan.monitor_type)
            self.plan.polling_intervals = intervals
            self.plan.polling_interval = intervals[self.plan.monitor_type]
            if self.replace_self:
                replacement = Monitor(self.plan.monitor_type, plan=self.plan)
                service.monitors.remove(self)
                service.monitors.append(replacement)

    service.monitors = [
        Monitor("redis", replace_self=True),
        Monitor("file"),
        Monitor("common"),
    ]

    service._apply_config(
        {
            "redis_monitor_polling_interval": "7",
            "file_monitor_polling_interval": "8",
            "common_monitor_polling_interval": "9",
        }
    )

    assert updates == ["redis", "file", "common"]
    assert sorted(
        (monitor.plan.monitor_type, monitor.plan.polling_interval)
        for monitor in service.monitors
    ) == [("common", 9), ("file", 8), ("redis", 7)]

    class Connector(object):
        def __init__(self):
            self.handler = None

        def get_table(self, table):
            assert table == "DLDD_CONFIG"
            return {
                "global": {
                    "redis_monitor_polling_interval": "7",
                    "file_monitor_polling_interval": "8",
                }
            }

        def subscribe(self, table, handler):
            self.handler = handler

        def listen(self):
            self.handler(
                "DLDD_CONFIG",
                "global",
                {"redis_monitor_polling_interval": "7"},
            )

    connector = Connector()
    observed = []
    ConfigDBProvider(connector).listen(observed.append)
    assert observed == [
        {
            "redis_monitor_polling_interval": "7",
            "file_monitor_polling_interval": "8",
        }
    ]

    queued = []
    service = object.__new__(DLDDService)
    service.paths = SimpleNamespace(defaults=str(tmp_path / "missing.yaml"))
    service.config = DLDDConfig()
    service.telemetry = SimpleNamespace(config=None)
    service.orchestrator = SimpleNamespace(
        config=None,
        queue_config_update=queued.append,
    )
    service.monitors = []
    service._apply_config({"inactive_fault_retention_period": "42"})
    assert service.config.inactive_fault_retention_period == 42
    assert queued == [service.config]


def test_crash_state_persists_only_broken_not_degraded_records():
    saved = []
    service = object.__new__(DLDDService)
    service.activation = SimpleNamespace(checksum="sha256:test")
    service.orchestrator = SimpleNamespace(
        broken_rules={
            "degraded": {"correlation_key": "degraded", "state": "DEGRADED"},
            "broken": {"correlation_key": "broken", "state": "BROKEN"},
        }
    )
    service.state_store = SimpleNamespace(
        save=lambda checksum, records, clean_shutdown: saved.append(
            (checksum, list(records), clean_shutdown)
        )
    )
    service._state_fingerprint = None

    service._persist_state_if_changed(force=True)

    assert saved == [
        (
            "sha256:test",
            [{"correlation_key": "broken", "state": "BROKEN"}],
            False,
        )
    ]
