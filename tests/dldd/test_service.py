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
from dldd.models import BrokenRule, ValidationIssue
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService, validate_runtime_operation_hooks
from dldd.validation import ExactCompatibilityMatcher


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
            "last_attempt": 1234.5,
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


def test_adapter_broken_rule_includes_last_attempt(tmp_path, monkeypatch):
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

    class RejectingAdapter(object):
        def validate(self, unused_item):
            raise ValueError("unsupported source binding")

    class CapturingTelemetry(object):
        def __init__(self, *args, **kwargs):
            self.status = None

        def publish_status(self, *args, **kwargs):
            self.status = (args, kwargs)

    def fake_build_plans(rules, *args):
        return SimpleNamespace(
            work_items={item.correlation_key: item} if tuple(rules) else {},
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
    monkeypatch.setattr(service, "_adapters", lambda: {"redis": RejectingAdapter()})
    monkeypatch.setattr(dldd_service.time, "time", lambda: 6789.0)

    service.start()

    assert service.startup_broken == (
        {
            "rule": "BAD_ADAPTER",
            "rule_id": 1000001,
            "version": "4.5.6",
            "correlation_key": item.correlation_key,
            "reason": "validation_error: unsupported source binding",
            "failure_count": 1,
            "state": "BROKEN",
            "last_attempt": 6789.0,
        },
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
