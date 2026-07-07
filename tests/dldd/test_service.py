from __future__ import absolute_import

import json
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd import service as dldd_service
from dldd.artifacts import (
    DEFAULT_ARTIFACT_DIRECTORY,
    ArtifactRequest,
    HealthzArtifactClient,
)
from dldd.config import ConfigDBProvider, DLDDConfig
from dldd.dse import DSERegistry
from dldd.hooks import VendorHook, VendorHookError, VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.models import BrokenRule, ValidationIssue, ValidationResult
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.planner import build_plans
from dldd.runtime import MonitorWorkState
from dldd.service import DLDDService, validate_runtime_operation_hooks
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


def test_service_composes_vendor_artifact_client_with_stable_named_args(tmp_path):
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


def test_service_rejects_vendor_artifact_client_with_wrong_type(tmp_path):
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


def test_service_simple_namespace_extensions_use_default_artifact_client(
    tmp_path, monkeypatch
):
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


def test_ingestion_broken_rule_includes_version_and_last_attempt(
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


def test_service_candidate_preflight_validates_without_reading(
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
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(
                id=1000001, name="NO_READ", version="1.0.0"
            ),
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(local_actions=None),
                log_collection=None,
            ),
        )
    )
    validation = SimpleNamespace(
        schema_version="0.0.1",
        materialized_rules=(materialized,),
        broken_rules=(),
        file_errors=(),
        file_valid=True,
        source_lines={"$": 1},
    )
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        rule_name="NO_READ",
        correlation_key="1000001:1",
    )
    calls = []

    class NoReadAdapter(object):
        def validate(self, received):
            calls.append(("validate", received.correlation_key))

        def get_value(self, unused_item):
            pytest.fail("service activation read a source value")

        def collect(self, unused_item):
            pytest.fail("service activation collected a source")

    monkeypatch.setattr(dldd_service, "load_rules", lambda *args: validation)
    monkeypatch.setattr(
        dldd_service,
        "build_plans",
        lambda *args, **kwargs: SimpleNamespace(
            work_items={item.correlation_key: item}
        ),
    )
    monkeypatch.setattr(
        service, "_adapters", lambda: {"redis": NoReadAdapter()}
    )

    candidate = service._validate_candidate("rules.yaml", "dse.yaml")

    assert candidate.activatable
    assert candidate.usable_rule_count == 1
    assert calls == [("validate", item.correlation_key)]


def test_service_candidate_preflight_propagates_adapter_programming_error(
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
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(
                id=1000001, name="BUGGY", version="1.0.0"
            ),
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(local_actions=None),
                log_collection=None,
            ),
        )
    )
    validation = ValidationResult(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(materialized,),
        source_lines={"$": 1},
    )
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        rule_name="BUGGY",
        correlation_key="1000001:1",
    )

    class BuggyAdapter(object):
        def validate(self, unused_item):
            raise RuntimeError("adapter implementation bug")

    monkeypatch.setattr(dldd_service, "load_rules", lambda *args: validation)
    monkeypatch.setattr(
        dldd_service,
        "build_plans",
        lambda *args, **kwargs: SimpleNamespace(
            work_items={item.correlation_key: item}
        ),
    )
    monkeypatch.setattr(
        service, "_adapters", lambda: {"redis": BuggyAdapter()}
    )

    with pytest.raises(RuntimeError, match="adapter implementation bug"):
        service._validate_candidate("rules.yaml", "dse.yaml")

    class RejectingAdapter(object):
        def validate(self, unused_item):
            raise ValueError("unsupported source binding")

    monkeypatch.setattr(
        service, "_adapters", lambda: {"redis": RejectingAdapter()}
    )
    monkeypatch.setattr(dldd_service.time, "time", lambda: 6789.0)

    candidate = service._validate_candidate("rules.yaml", "dse.yaml")

    assert candidate.usable_rule_count == 0
    assert candidate.broken_rules[0]["last_attempt"] == 6789.0
    assert "unsupported source binding" in candidate.broken_rules[0]["reason"]


def test_service_candidate_records_remain_within_external_byte_cap():
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
    result = SimpleNamespace(broken_rules=broken_rules)

    records = dldd_service._bounded_broken_rule_records(result, 1234.5)
    serialized = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert len(records) == 1024
    assert len(serialized) <= dldd_service.MAX_SERIALIZED_DIAGNOSTIC_BYTES
    assert len({record["rule"] for record in records}) == 1024
    assert all("\0" not in record["rule"] for record in records)


def test_rule_status_snapshot_aggregates_health_faults_and_work_details():
    result = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 41, "file": 42, "common": 43},
    )
    item = next(iter(bundle.work_items.values()))
    plan = bundle.monitor_plans["redis"]
    state = plan.state_by_key[item.correlation_key]
    state.state = MonitorWorkState.DEGRADED
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
    service = object.__new__(DLDDService)
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
    assert active["work_items"][0]["active_fault"]
    assert rows[1]["health"] == "BROKEN"
    assert rows[1]["work_items_total"] == 0


@pytest.mark.parametrize(
    "work_state,expected_health",
    (
        (MonitorWorkState.READY, "OK"),
        (MonitorWorkState.SUSPENDED, "SUSPENDED"),
        (MonitorWorkState.BROKEN, "BROKEN"),
        (MonitorWorkState.DEGRADED, "DEGRADED"),
    ),
)
def test_rule_status_snapshot_classifies_work_state(
    work_state, expected_health
):
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

    rows, truncated = service._rule_status_snapshot()

    assert not truncated
    assert rows[0]["health"] == expected_health


def test_rule_status_snapshot_groups_ingestion_failures_by_rule():
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
    service = object.__new__(DLDDService)
    service.activation = SimpleNamespace(
        payload=SimpleNamespace(materialized_rules=()),
        broken_rules=failures,
        checksum="sha256:test",
    )
    service.orchestrator = None
    service.monitors = []

    rows, truncated = service._rule_status_snapshot()

    assert not truncated
    assert len(rows) == 1
    assert rows[0]["rule"] == "BAD_RULE"
    assert rows[0]["health"] == "BROKEN"
    assert rows[0]["failure_count"] == 2
    assert rows[0]["last_attempt"] == 1001.0
    assert rows[0]["reason"] == "first problem (+1 more)"


def test_rule_status_snapshot_reports_omitted_detail(monkeypatch):
    result = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        result.materialized_rules,
        "sha256:test",
        {"redis": 41, "file": 42, "common": 43},
    )
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
    service.monitors = [
        SimpleNamespace(plan=bundle.monitor_plans["redis"])
    ]
    monkeypatch.setattr("dldd.rule_status.MAX_DETAILS_PER_RULE", 0)

    rows, truncated = service._rule_status_snapshot()

    assert truncated
    assert rows[0]["work_items"] == []
    assert rows[0]["work_items_omitted"] == 1


def test_rule_status_publication_failure_does_not_break_heartbeat(caplog):
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

    service._publish_rule_status()

    assert not published
    assert "unable to build DLDD rule status snapshot" in caplog.text


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


def test_dynamic_config_updates_runtime_and_monitor_intervals(tmp_path):
    service = object.__new__(DLDDService)
    service.paths = SimpleNamespace(defaults=str(tmp_path / "missing.yaml"))
    service.config = DLDDConfig()
    service.telemetry = SimpleNamespace(config=None)
    service.orchestrator = SimpleNamespace(config=None)
    service.monitors = [
        SimpleNamespace(
            plan=SimpleNamespace(monitor_type=monitor_type, polling_interval=60),
            fault_evidence_ack_timeout=120,
            source_recovery_samples=1,
        )
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


def test_dynamic_config_uses_stable_monitor_snapshot_during_replacement(tmp_path):
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

        def update_polling_interval(self, interval):
            updates.append(self.plan.monitor_type)
            self.plan.polling_interval = interval
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


def test_config_notification_rereads_complete_global_row():
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


def test_runtime_config_update_is_queued_for_primary_owner(tmp_path):
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


def test_activation_preflight_rejects_custom_action_without_runtime_hook():
    operation = SimpleNamespace(
        type="vendor_reset", executor=None, options={}
    )
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(
                    local_actions=SimpleNamespace(action_list=(operation,))
                ),
                log_collection=None,
            )
        )
    )

    with pytest.raises(VendorHookError, match="not registered"):
        validate_runtime_operation_hooks(materialized, VendorHookRegistry())


def test_activation_preflight_rejects_custom_query_without_runtime_hook():
    operation = SimpleNamespace(
        type="vendor_dump", executor=None, options={"hook": "diagnostics"}
    )
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(local_actions=None),
                log_collection=SimpleNamespace(queries=(operation,)),
            )
        )
    )

    with pytest.raises(VendorHookError, match="diagnostics"):
        validate_runtime_operation_hooks(materialized, VendorHookRegistry())


class RuntimeOperationHook(VendorHook):
    def collect(self, operation):
        return None

    def execute_action(self, action):
        return {}


class RejectingI2CHook(RuntimeOperationHook):
    def validate_source(self, operation):
        raise ValueError("logical bus is not mapped")


def test_activation_preflight_accepts_registered_operation_hook():
    action = SimpleNamespace(
        type="vendor_reset", executor=None, options={"hook": "operations"}
    )
    query = SimpleNamespace(
        type="vendor_dump", executor=None, options={"hook": "operations"}
    )
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(
                    local_actions=SimpleNamespace(action_list=(action,))
                ),
                log_collection=SimpleNamespace(queries=(query,)),
            )
        )
    )
    hooks = VendorHookRegistry()
    hooks.register("operations", RuntimeOperationHook())

    validate_runtime_operation_hooks(materialized, hooks)


def test_activation_preflight_runs_optional_i2c_hook_validation():
    operation = SimpleNamespace(
        type="i2c",
        executor=None,
        options={},
        path={"bus": "IO-MUX-6"},
    )
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(
                    local_actions=SimpleNamespace(action_list=(operation,))
                ),
                log_collection=None,
            )
        )
    )
    hooks = VendorHookRegistry()
    hooks.register("i2c", RejectingI2CHook())

    with pytest.raises(ValueError, match="not mapped"):
        validate_runtime_operation_hooks(materialized, hooks)


def test_activation_dry_run_validates_adapter_without_reading(
    monkeypatch, capsys
):
    calls = []

    class NoReadAdapter(object):
        def validate(self, item):
            calls.append(("validate", item.correlation_key))

        def get_value(self, unused_item):
            pytest.fail("activation dry-run read a source value")

        def collect(self, unused_item):
            pytest.fail("activation dry-run collected a source")

    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(id=1000001, name="NO_READ"),
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(local_actions=None),
                log_collection=None,
            ),
        )
    )
    result = SimpleNamespace(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(materialized,),
        broken_rules=(),
        file_errors=(),
        file_valid=True,
        activation_valid=True,
        source_lines={"$": 1},
    )
    item = SimpleNamespace(
        source_type="redis",
        rule_id=1000001,
        correlation_key="1000001:1",
    )
    extensions = SimpleNamespace(
        dse_registry=SimpleNamespace(source_types=()),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=SimpleNamespace(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: PlatformIdentity("test", "product", "software"),
    )
    monkeypatch.setattr(dldd_cli, "load_extensions", lambda *args: extensions)
    monkeypatch.setattr(dldd_cli, "load_rules", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        dldd_cli,
        "build_plans",
        lambda *args, **kwargs: SimpleNamespace(
            work_items={item.correlation_key: item}
        ),
    )
    monkeypatch.setattr(
        dldd_cli, "adapter_map", lambda **kwargs: {"redis": NoReadAdapter()}
    )
    args = SimpleNamespace(
        mode="activation-dry-run",
        platform_dir=None,
        dse=None,
        file="rules.yaml",
        json=True,
        verbose=False,
    )

    assert dldd_cli.validate_rules(args) == 0
    assert calls == [("validate", item.correlation_key)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["probe_results"] == [
        {"correlation_key": item.correlation_key, "state": "VALID"}
    ]

    class BuggyAdapter(object):
        def validate(self, unused_item):
            raise RuntimeError("adapter implementation bug")

    monkeypatch.setattr(
        dldd_cli,
        "adapter_map",
        lambda **kwargs: {"redis": BuggyAdapter()},
    )
    with pytest.raises(RuntimeError, match="adapter implementation bug"):
        dldd_cli.validate_rules(args)


def test_activation_dry_run_reports_missing_runtime_operation_hook(
    monkeypatch, capsys
):
    operation = SimpleNamespace(
        type="vendor_reset", executor=None, options={}
    )
    materialized = SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(id=1000001, name="VENDOR_RESET"),
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(
                    local_actions=SimpleNamespace(action_list=(operation,))
                ),
                log_collection=None,
            ),
        )
    )
    result = SimpleNamespace(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(materialized,),
        broken_rules=(),
        file_errors=(),
        file_valid=True,
        activation_valid=True,
        source_lines={"$": 1, "$.signatures": 2},
    )
    extensions = SimpleNamespace(
        dse_registry=SimpleNamespace(source_types=()),
        vendor_hooks=VendorHookRegistry(),
        compatibility_matcher=SimpleNamespace(),
    )
    monkeypatch.setattr(
        dldd_cli,
        "detect_identity",
        lambda: PlatformIdentity("test", "product", "software"),
    )
    monkeypatch.setattr(dldd_cli, "load_extensions", lambda *args: extensions)
    monkeypatch.setattr(dldd_cli, "load_rules", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        dldd_cli,
        "build_plans",
        lambda *args, **kwargs: SimpleNamespace(work_items={}),
    )
    monkeypatch.setattr(dldd_cli, "adapter_map", lambda **kwargs: {})
    args = SimpleNamespace(
        mode="activation-dry-run",
        platform_dir=None,
        dse=None,
        file="rules.yaml",
        json=True,
        verbose=False,
    )

    status = dldd_cli.validate_rules(args)
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload["rules_parsed_successfully"] == 0
    assert payload["rule_level_result"] == "FAILED"
    assert payload["broken_rules"][0]["issues"][0]["code"] == (
        "activation_preflight_failed"
    )
    assert payload["broken_rules"][0]["issues"][0]["line"] == 2

    args.json = False
    status = dldd_cli.validate_rules(args)
    output = capsys.readouterr().out

    assert status == 1
    assert "Rules failed validation: 1" in output
    assert "activation_preflight_failed" in output
    assert "vendor hook is not registered: vendor_reset" in output
    assert "(line 2)" in output
