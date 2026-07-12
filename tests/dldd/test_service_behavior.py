from __future__ import absolute_import

import signal
from types import SimpleNamespace

import pytest

from dldd import service as dldd_service
from dldd.config import DLDDConfig
from dldd.dse import DSERegistry
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.models import BrokenRule, ValidationIssue
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.runtime import MonitorWorkState
from dldd.service import DLDDService, TelemetryUnavailable
from dldd.validation import ExactCompatibilityMatcher


def _extensions(hooks=None):
    return PlatformExtensions(
        PlatformIdentity("test-platform", "product", "software"),
        DSERegistry(),
        hooks or VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )


def _service(tmp_path, hooks=None):
    return DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=_extensions(hooks),
    )


def test_service_no_rules_and_config_fallback_startup_contract(
    tmp_path, monkeypatch, caplog
):
    service = DLDDService(
        paths=RulePaths(
            str(tmp_path / "platform"),
            inbox=str(tmp_path / "inbox" / "dld_rules.yaml"),
            rules_dir=str(tmp_path / "rules"),
            state_file=str(tmp_path / "dld_state.json"),
        ),
        state_db=object(),
        extensions=_extensions(),
    )
    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service,
        "TelemetryPublisher",
        lambda *args, **kwargs: object(),
    )
    shutdown_modes = []
    monkeypatch.setattr(
        service,
        "shutdown",
        lambda clean_shutdown=True: shutdown_modes.append(clean_shutdown),
    )

    service.run()

    assert service.stop_event.is_set()
    assert service.activation is None
    assert service.fatal_reason == ""
    assert shutdown_modes == [True]
    assert not (tmp_path / "rules" / "activation.json").exists()


    service = _service(tmp_path)
    service.config_provider = SimpleNamespace(
        load=lambda: (_ for _ in ()).throw(RuntimeError("CONFIG_DB unavailable"))
    )
    monkeypatch.setattr(
        dldd_service,
        "load_vendor_defaults",
        lambda unused_path: (_ for _ in ()).throw(ValueError("invalid defaults")),
    )

    config = service._load_config()

    assert config == DLDDConfig()
    assert "unable to load DLDD_CONFIG: CONFIG_DB unavailable" in caplog.text
    assert "invalid vendor defaults: invalid defaults" in caplog.text


def test_candidate_diagnostics_and_start_failure_status_contract(
    tmp_path, monkeypatch
):
    issues = tuple(
        ValidationIssue("file", "invalid_document", "error {}".format(index))
        for index in range(257)
    )

    strings = dldd_service._bounded_file_error_strings(issues)

    assert len(strings) == 257
    assert strings[-1] == "additional file diagnostics were omitted"

    broken = BrokenRule(
        rule_name="BROKEN",
        rule_id=1000001,
        rule_version="1.0.0",
        issues=(ValidationIssue("rule", "unknown_field", "X" * 4096),),
    )
    monkeypatch.setattr(dldd_service, "MAX_SERIALIZED_DIAGNOSTIC_BYTES", 32768)

    records = dldd_service._bounded_broken_rule_records(
        SimpleNamespace(broken_rules=(broken,)),
        1234.9,
    )

    assert records[0]["reason"] == (
        "validation details omitted by candidate byte cap"
    )


    service = _service(tmp_path)
    published = []
    service.activation = SimpleNamespace(broken_rules=({"rule": "BAD"},))
    service._publish_status = lambda: published.append(True) or True

    service._fail_start("cannot construct monitors")

    assert service.fatal_reason == "cannot construct monitors"
    assert service.startup_broken == ({"rule": "BAD"},)
    assert published == [True]

    service.activation = None
    service.startup_broken = ()
    service._fail_start("rules activation failed")
    assert service.startup_broken == ()

    service = _service(tmp_path)

    assert service._activation_status_fields() == {}

    service.activation = SimpleNamespace(
        payload=SimpleNamespace(ruleset=None),
        source="inbox",
        validation_result="FALLBACK",
        fallback_used=True,
        previous_checksum="sha256:old",
    )

    assert service._activation_status_fields() == {
        "local_action_default_timeout": None,
        "active_rules_source": "inbox",
        "activation_result": "FALLBACK",
        "activation_fallback_used": True,
        "previous_active_rules_checksum": "sha256:old",
    }


def test_activation_adapter_registry_and_start_rejection_contract(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    sentinel = {"redis": object()}
    observed = []
    monkeypatch.setattr(
        dldd_service,
        "build_adapter_registry",
        lambda extensions: observed.append(extensions) or sentinel,
    )

    assert service._adapters() is sentinel
    assert observed == [service.extensions]


    service = _service(tmp_path)
    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service,
        "TelemetryPublisher",
        lambda *unused_args, **unused_kwargs: object(),
    )
    monkeypatch.setattr(
        dldd_service.RuleGenerationManager,
        "activate",
        lambda unused_manager: (_ for _ in ()).throw(
            RuntimeError("candidate checksum mismatch")
        ),
    )
    failures = []
    monkeypatch.setattr(service, "_fail_start", failures.append)

    service.start()

    assert failures == ["candidate checksum mismatch"]
    assert service.activation is None
    assert service.monitors == []

    service = _service(tmp_path)
    activation = SimpleNamespace(
        payload=SimpleNamespace(materialized_rules=()),
        checksum="sha256:test",
    )
    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service,
        "TelemetryPublisher",
        lambda *unused_args, **unused_kwargs: object(),
    )
    monkeypatch.setattr(
        dldd_service.RuleGenerationManager,
        "activate",
        lambda unused_manager: activation,
    )
    monkeypatch.setattr(
        dldd_service,
        "build_plans",
        lambda *unused_args: SimpleNamespace(work_items={}, templates={}),
    )
    monkeypatch.setattr(service, "_adapters", lambda: {})
    failures = []
    monkeypatch.setattr(service, "_fail_start", failures.append)

    service.start()

    assert failures == ["zero usable monitor work items after activation"]
    assert service.action_runner is None


@pytest.mark.parametrize(
    "async_origin", ("none", "item", "template", "template-common")
)
@pytest.mark.parametrize("cancel_reconciliation", (False, True))
def test_start_composes_runtime_and_restores_only_matching_broken_work(
    tmp_path, monkeypatch, async_origin, cancel_reconciliation
):
    service = _service(tmp_path)
    item = SimpleNamespace(
        correlation_key="work:1",
        async_collection=async_origin == "item",
    )
    template = SimpleNamespace(
        item=SimpleNamespace(async_collection=async_origin == "template"),
        common_items=(
            SimpleNamespace(
                async_collection=async_origin == "template-common"
            ),
        ),
    )
    ruleset = SimpleNamespace(local_action_default_timeout=17)
    validation = SimpleNamespace(
        materialized_rules=(SimpleNamespace(),),
        ruleset=ruleset,
    )
    activation = SimpleNamespace(
        payload=validation,
        checksum="sha256:test",
        broken_rules=({"rule": "INGESTION_BAD"},),
    )
    plan = SimpleNamespace(monitor_id="redis")
    bundle = SimpleNamespace(
        work_items={item.correlation_key: item},
        templates=(
            {"template:1": template}
            if async_origin in ("template", "template-common")
            else {}
        ),
        monitor_plans={"redis": plan},
        signatures={},
    )
    created = SimpleNamespace(
        monitors=[],
        persisted=[],
        published=0,
        reconciled=0,
        commands=[],
        config_threads=0,
    )

    class FakeTelemetry(object):
        def __init__(self, *unused_args, **unused_kwargs):
            pass

    class FakeActionRunner(object):
        def __init__(self, unused_executor):
            pass

    async_pool = object()

    class FakeOrchestrator(object):
        def __init__(self, *unused_args, **kwargs):
            assert kwargs["local_action_default_timeout"] == 17
            self.broken_rules = {}
            self.service_diagnostics = []

        def _command_key(self, *args):
            created.commands.append(args)

        def reconcile_existing_faults(self):
            created.reconciled += 1

    class FakeMonitor(object):
        def __init__(self, received_plan):
            self.plan = received_plan
            self.started = False

        def start(self):
            self.started = True

    class FakeThread(object):
        def __init__(self, **kwargs):
            assert kwargs["name"] == "dldd-config"

        def start(self):
            created.config_threads += 1

    monkeypatch.setattr(service, "_load_config", lambda: DLDDConfig())
    monkeypatch.setattr(
        dldd_service.RuleGenerationManager,
        "activate",
        lambda unused_manager: activation,
    )
    monkeypatch.setattr(dldd_service, "TelemetryPublisher", FakeTelemetry)
    monkeypatch.setattr(dldd_service, "build_plans", lambda *args: bundle)
    monkeypatch.setattr(service, "_adapters", lambda: {"redis": object()})
    monkeypatch.setattr(service, "_create_artifact_client", lambda: object())
    monkeypatch.setattr(dldd_service, "ActionRunner", FakeActionRunner)
    monkeypatch.setattr(
        dldd_service, "ActionExecutor", lambda **unused_kwargs: object()
    )
    monkeypatch.setattr(
        dldd_service, "AsyncCollectionPool", lambda: async_pool
    )
    monkeypatch.setattr(
        dldd_service, "CorrelationEngine", lambda unused_signatures: object()
    )
    monkeypatch.setattr(dldd_service, "PrimaryOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        service.state_store,
        "load",
        lambda *args, **kwargs: {
            "recovery_error": (
                "state checksum changed" if async_origin != "none" else ""
            ),
            "broken_rules": (
                {"correlation_key": "work:1", "state": "BROKEN"},
                {"correlation_key": "missing", "state": "BROKEN"},
                {"correlation_key": "work:1", "state": "DEGRADED"},
            ),
        },
    )
    monkeypatch.setattr(
        service,
        "_new_monitor",
        lambda received_plan: (
            created.monitors.append(FakeMonitor(received_plan))
            or created.monitors[-1]
        ),
    )
    monkeypatch.setattr(dldd_service.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        service,
        "_persist_state_if_changed",
        lambda force=False: created.persisted.append(force),
    )
    monkeypatch.setattr(
        service,
        "_publish_status",
        lambda: setattr(created, "published", created.published + 1) or True,
    )
    if cancel_reconciliation:
        monkeypatch.setattr(
            service, "_reconcile_existing_faults_at_startup", lambda: False
        )

    service.start()

    if cancel_reconciliation:
        assert not created.monitors
        assert not created.persisted
        assert created.config_threads == 0
        return

    assert created.reconciled == 1
    assert created.commands[0][0] == "work:1"
    assert created.commands[0][1].value == "SUSPEND"
    assert created.commands[0][2] is MonitorWorkState.BROKEN
    assert created.monitors[0].started
    assert created.persisted == [True]
    # The run loop owns and counts the first publication attempt.
    assert created.published == 0
    assert created.config_threads == 1
    assert service.async_collection_pool is (
        async_pool if async_origin != "none" else None
    )
    if async_origin == "none":
        assert not service.orchestrator.service_diagnostics
    else:
        assert service.orchestrator.service_diagnostics[0]["reason"] == (
            "broken_rule_state_not_restored"
        )


def test_new_monitor_receives_the_shared_runtime_dependencies(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    service.config = DLDDConfig(
        fault_evidence_ack_timeout=17, source_recovery_samples=3
    )
    service.adapters = {"redis": object()}
    service.evidence_queue = object()
    service.async_collection_pool = object()
    captured = {}
    created = object()

    def monitor_factory(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return created

    monkeypatch.setattr(dldd_service, "MonitorThread", monitor_factory)
    plan = object()

    assert service._new_monitor(plan) is created
    assert captured["args"] == (plan, service.adapters, service.evidence_queue)
    assert captured["kwargs"] == {
        "fault_evidence_ack_timeout": 17,
        "source_recovery_samples": 3,
        "async_collection_pool": service.async_collection_pool,
        "stop_event": service.stop_event,
    }


class RecordingHook(VendorHook):
    def __init__(self, result):
        self.result = result
        self.operations = []

    def collect(self, operation):
        self.operations.append(operation)
        return self.result

    def collect_query(self, operation):
        self.operations.append(operation)
        return self.result

    def execute_action(self, operation):
        self.operations.append(operation)
        return self.result


def test_platform_artifact_query_and_runtime_hook_contract(
    tmp_path, monkeypatch
):
    hook = RecordingHook("vendor-result")
    hooks = VendorHookRegistry()
    hooks.register("diagnostics", hook)
    service = _service(tmp_path, hooks)
    filesystem_calls = []
    monkeypatch.setattr(
        dldd_service.FilesystemArtifactClient,
        "_run_query",
        lambda query: filesystem_calls.append(query) or "filesystem-result",
    )

    executor_query = {"executor": lambda operation: operation}
    cli_query = {"type": "cli", "command": "show version"}
    vendor_query = {"type": "vendor", "hook": "diagnostics"}

    assert service._run_artifact_query(executor_query) == "filesystem-result"
    assert service._run_artifact_query(cli_query) == "filesystem-result"
    assert service._run_artifact_query(vendor_query) == "vendor-result"
    assert filesystem_calls == [executor_query, cli_query]
    assert hook.operations == [vendor_query]


    for hook_result, expected in (
        ({"serial_number": "SERIAL-1"}, "SERIAL-1"),
        (1234, "1234"),
        (None, ""),
    ):
        hook = RecordingHook(hook_result)
        hooks = VendorHookRegistry()
        hooks.register("component_metadata", hook)
        service = _service(tmp_path, hooks)

        assert service._component_serial("FAN", "FAN0") == expected
        assert service._component_serial("FAN", "FAN0") == expected
        assert len(hook.operations) == 1

    service = _service(tmp_path)
    item = SimpleNamespace(
        source_id="source",
        source_type="redis",
        source={},
        component_name="SENSOR0",
    )

    assert service._component_serial("FAN", "FAN0") == ""
    assert service._source_is_in_expected_maintenance(item) is False

    for hook_result, expected in (
        ({"graceful": True}, True),
        ({"suspended": True}, True),
        ({"graceful": False, "suspended": True}, False),
        (1, True),
        (0, False),
    ):
        hook = RecordingHook(hook_result)
        hooks = VendorHookRegistry()
        hooks.register("source_lifecycle", hook)
        service = _service(tmp_path, hooks)
        item = SimpleNamespace(
            source_id="source",
            source_type="redis",
            source={"key": "SENSOR0"},
            component_name="SENSOR0",
        )

        assert service._source_is_in_expected_maintenance(item) is expected
        assert hook.operations[0]["operation"] == "is_expected_maintenance"


def test_config_listener_and_invalid_update_contract(
    tmp_path, monkeypatch, caplog
):
    service = _service(tmp_path)

    class OneCycleEvent(object):
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            assert timeout == 5
            self.stopped = True

    calls = []
    service.stop_event = OneCycleEvent()
    service.config_provider = SimpleNamespace(
        listen=lambda callback: (_ for _ in ()).throw(
            RuntimeError("subscription disconnected")
        ),
        reset=lambda: calls.append("reset"),
    )

    service._listen_for_config()

    assert calls == ["reset"]
    assert "DLDD_CONFIG subscription failed: subscription disconnected" in caplog.text


    service = _service(tmp_path)

    class OneCycleEvent(object):
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.stopped = True

    updates = []
    service.stop_event = OneCycleEvent()
    service._apply_config = updates.append

    def listen(callback):
        callback({"redis_monitor_polling_interval": "9"})

    service.config_provider = SimpleNamespace(listen=listen, reset=lambda: None)

    service._listen_for_config()

    assert updates == [{"redis_monitor_polling_interval": "9"}]


    service = _service(tmp_path)

    def listen(unused_callback):
        service.stop_event.set()
        raise RuntimeError("subscription interrupted by shutdown")

    resets = []
    service.config_provider = SimpleNamespace(
        listen=listen,
        reset=lambda: resets.append(True),
    )

    service._listen_for_config()

    assert resets == [True]
    assert "subscription interrupted by shutdown" not in caplog.text


    service = _service(tmp_path)
    original = service.config
    service.telemetry = SimpleNamespace(config=original)
    service.orchestrator = SimpleNamespace(config=original)
    service.monitors = []
    monkeypatch.setattr(
        dldd_service,
        "load_vendor_defaults",
        lambda unused_path: (_ for _ in ()).throw(
            ValueError("vendor defaults are malformed")
        ),
    )

    service._apply_config({"redis_monitor_polling_interval": "7"})

    assert service.config is original
    assert service.telemetry.config is original
    assert service.orchestrator.config is original
    assert "ignoring invalid DLDD_CONFIG update" in caplog.text


def test_run_processes_batches_publishes_and_shuts_down_cleanly(monkeypatch):
    service = object.__new__(DLDDService)

    class TwoCycleEvent(object):
        def __init__(self):
            self.waits = []

        def is_set(self):
            return len(self.waits) >= 2

        def wait(self, timeout):
            self.waits.append(timeout)

    event = TwoCycleEvent()
    service.stop_event = event
    service.start = lambda: None
    batches = iter((1, 0))
    service.orchestrator = SimpleNamespace(
        process_batch=lambda: next(batches)
    )
    supervised = []
    persisted = []
    published = []
    shutdown = []
    service._supervise_monitors = lambda: supervised.append(True)
    service._persist_state_if_changed = lambda: persisted.append(True)
    service._publish_status = lambda: published.append(True) or True
    service.shutdown = lambda clean_shutdown=True: shutdown.append(clean_shutdown)
    clock = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(dldd_service.time, "monotonic", lambda: next(clock))

    service.run()

    assert event.waits == [0, 0.2]
    assert supervised == [True, True]
    assert persisted == [True]
    assert published == [True]
    assert shutdown == [True]


def test_monitor_supervision_restarts_only_dead_monitors(tmp_path, caplog):
    service = _service(tmp_path)
    service.adapters = {"redis": object()}
    service.evidence_queue = object()
    service.stop_event = SimpleNamespace(is_set=lambda: False)

    class Monitor(object):
        def __init__(self, monitor_id, alive):
            self.plan = SimpleNamespace(monitor_id=monitor_id)
            self.diagnostics = [{"reason": "old diagnostic"}]
            self.alive = alive
            self.started = False

        def is_alive(self):
            return self.alive

        def start(self):
            self.started = True

    alive = Monitor("alive", True)
    dead = Monitor("dead", False)
    replacement = Monitor("dead", True)
    replacement.diagnostics = []
    service.monitors = [alive, dead]
    service._new_monitor = lambda plan: replacement

    service._supervise_monitors()

    assert service.monitors == [alive, replacement]
    assert replacement.started
    assert replacement.diagnostics[0] == {"reason": "old diagnostic"}
    assert "stopped unexpectedly and was restarted" in (
        replacement.diagnostics[1]["reason"]
    )
    assert "restarting stopped DLDD monitor dead" in caplog.text


    service = _service(tmp_path)
    service.adapters = None
    service.evidence_queue = object()
    service.monitors = [SimpleNamespace()]
    service._supervise_monitors()

    service.adapters = {}
    service.evidence_queue = object()
    service.stop_event.set()
    dead = SimpleNamespace(
        is_alive=lambda: False,
        plan=SimpleNamespace(monitor_id="dead"),
    )
    service.monitors = [dead]
    service._new_monitor = lambda plan: pytest.fail(
        "monitor restarted during shutdown"
    )
    service._supervise_monitors()


def test_persist_state_skips_uninitialized_and_unchanged_state(tmp_path):
    service = _service(tmp_path)
    saved = []
    service.state_store = SimpleNamespace(
        save=lambda *args, **kwargs: saved.append((args, kwargs))
    )

    service._persist_state_if_changed()
    assert saved == []

    service.activation = SimpleNamespace(checksum="sha256:test")
    service.orchestrator = SimpleNamespace(broken_rules={})
    service._state_fingerprint = "[]"
    service._persist_state_if_changed()
    assert saved == []


class CapturingTelemetry(object):
    def __init__(self):
        self.statuses = []
        self.rule_statuses = []
        self.cleared = 0

    def publish_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))
        return True

    def publish_rule_status(self, *args, **kwargs):
        self.rule_statuses.append((args, kwargs))
        return True

    def clear_rule_status(self):
        self.cleared += 1
        return True


def test_status_publication_contract(tmp_path, monkeypatch):
    service = _service(tmp_path)

    assert service._publish_rule_status() is False

    telemetry = CapturingTelemetry()
    service.telemetry = telemetry
    assert service._publish_rule_status() is True
    assert telemetry.cleared == 1

    service.activation = SimpleNamespace(checksum="sha256:test")
    service._rule_status_snapshot = lambda: ([{"rule": "OK"}], True)
    assert service._publish_rule_status() is True
    assert telemetry.rule_statuses == [
        (("sha256:test", [{"rule": "OK"}]), {"detail_truncated": True})
    ]

    service = _service(tmp_path)
    assert service._publish_status() is False

    telemetry = CapturingTelemetry()
    service.telemetry = telemetry
    service._publish_rule_status = lambda: True

    assert service._publish_status() is True
    assert telemetry.statuses[-1][0][0] == "BROKEN|FATAL"
    assert telemetry.statuses[-1][1]["reason"] == "no active rules generation"

    service.activation = SimpleNamespace(
        schema_version="0.0.1",
        active_file="rules.yaml",
        checksum="sha256:test",
        broken_rules=({"rule": "ACTIVATION_BAD"},),
        payload=SimpleNamespace(
            ruleset=SimpleNamespace(local_action_default_timeout=12)
        ),
    )
    service.startup_broken = ({"rule": "STARTUP_BAD"},)
    service.fatal_reason = "monitor construction failed"
    assert service._publish_status() is True
    assert telemetry.statuses[-1][0][0] == "BROKEN|FATAL"
    assert telemetry.statuses[-1][1]["broken_rules"] == service.startup_broken

    now = 1000.0
    monkeypatch.setattr(dldd_service.time, "time", lambda: now)
    service.fatal_reason = ""
    service.monitors = [SimpleNamespace(diagnostics=({"reason": "monitor"},))]
    service._inflight_status = lambda: ({"state": "HELD_BY_PRIMARY"},)
    service.orchestrator = SimpleNamespace(
        source_status={
            "expired": {"state": "RECOVERED", "since": 900},
            "recent": {"state": "RECOVERED", "since": 990},
            "failed": {"state": "UNAVAILABLE", "since": 995},
        },
        broken_rules={"bad": {"rule": "RUNTIME_BAD"}},
        service_state=lambda: "DEGRADED",
        service_diagnostics=({"reason": "orchestrator"},),
        correlation=SimpleNamespace(
            diagnostics=({"reason": "correlation"},)
        ),
    )

    assert service._publish_status() is True
    args, kwargs = telemetry.statuses[-1]
    assert args[0] == "DEGRADED"
    assert "expired" not in service.orchestrator.source_status
    assert {item["state"] for item in kwargs["source_status"]} == {
        "RECOVERED",
        "UNAVAILABLE",
    }
    assert kwargs["inflight_fault_evidence"] == (
        {"state": "HELD_BY_PRIMARY"},
    )
    assert [item["reason"] for item in kwargs["service_diagnostics"]] == [
        "monitor",
        "orchestrator",
        "correlation",
    ]
    assert kwargs["reason"] == "DLDD has degraded or broken rules/sources"

    service.orchestrator = None
    service.activation.broken_rules = ()
    assert service._publish_status() is True
    args, kwargs = telemetry.statuses[-1]
    assert args[0] == "OK"
    assert kwargs["reason"] == ""


@pytest.mark.parametrize("completed_action", (True, False))
def test_inflight_status_includes_pending_local_action_progress(
    monkeypatch, completed_action
):
    key = "1000001:1:SENSOR0"
    item = SimpleNamespace(
        rule_name="ACTION_RULE",
        rule_id=1000001,
        event_id=1,
        component_name="SENSOR0",
    )
    state = SimpleNamespace(
        state=MonitorWorkState.HELD_BY_PRIMARY,
        last_enqueue_timestamp=990,
        hold_deadline=None,
    )
    plan = SimpleNamespace(
        monitor_id="redis",
        runtime_snapshot=lambda: ({key: item}, {key: state}),
    )
    execution = object()
    action_result = (
        SimpleNamespace(worker_id="worker-7", last_error="action failed")
        if completed_action
        else None
    )
    pending = SimpleNamespace(
        execution=execution,
        action_result=action_result,
        wait_until=150.0 if completed_action else None,
        phase="ACTIONS" if completed_action else "RECHECK",
        future=SimpleNamespace(dldd_worker_id="worker-8"),
        first_decision=SimpleNamespace(
            event=SimpleNamespace(event_timestamp=975)
        ),
    )
    service = object.__new__(DLDDService)
    service.monitors = [SimpleNamespace(plan=plan)]
    service.orchestrator = SimpleNamespace(
        pending={"pending": pending},
        _execution_keys=lambda received: (
            (key,) if received is execution else ()
        ),
    )
    monkeypatch.setattr(dldd_service.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(dldd_service.time, "time", lambda: 1000.0)

    status = service._inflight_status()[0]["local_action_state"]

    if completed_action:
        assert status == {
            "state": "RUNNING",
            "worker_id": "worker-7",
            "started_at": 975,
            "wait_until": 1050.0,
            "last_error": "action failed",
        }
    else:
        assert status == {
            "state": "WAITING_FOR_RECHECK",
            "worker_id": "worker-8",
            "started_at": 975,
            "wait_until": None,
            "last_error": "",
        }


def test_shutdown_and_signal_handler_contract(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    calls = []
    service.monitors = [
        SimpleNamespace(join=lambda timeout: calls.append(("monitor", timeout)))
    ]
    service.action_runner = SimpleNamespace(
        shutdown=lambda wait: calls.append(("action", wait))
    )
    service.async_collection_pool = SimpleNamespace(
        shutdown=lambda wait: calls.append(("async", wait))
    )
    service.artifact_client = SimpleNamespace(
        shutdown=lambda wait: calls.append(("artifact", wait))
    )
    service.activation = SimpleNamespace(checksum="sha256:test")
    service.orchestrator = SimpleNamespace(
        broken_rules={
            "broken": {"state": "BROKEN"},
            "degraded": {"state": "DEGRADED"},
        }
    )
    saved = []
    service.state_store = SimpleNamespace(
        save=lambda checksum, records, clean_shutdown: saved.append(
            (checksum, list(records), clean_shutdown)
        )
    )

    service.shutdown(clean_shutdown=True)

    assert service.stop_event.is_set()
    assert calls == [
        ("monitor", 5),
        ("action", False),
        ("async", False),
        ("artifact", False),
    ]
    assert saved == [("sha256:test", [{"state": "BROKEN"}], True)]


    service = _service(tmp_path)

    service.shutdown()

    assert service.stop_event.is_set()


    handlers = {}
    observed = []

    def register(signum, handler):
        handlers[signum] = handler

    class FakeService(object):
        def __init__(self, stop_event):
            self.stop_event = stop_event

        def run(self):
            assert not self.stop_event.is_set()
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            observed.append(self.stop_event.is_set())

    monkeypatch.setattr(dldd_service.signal, "signal", register)
    monkeypatch.setattr(dldd_service, "DLDDService", FakeService)

    dldd_service.run_service()

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert observed == [True]


def test_startup_fault_reconciliation_retry_contract(
    tmp_path, monkeypatch, caplog
):
    """Cover recovery, shutdown cancellation, and the bounded fatal path."""

    service = _service(tmp_path)
    calls = []

    def reconcile():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary STATE_DB read failure")

    service.orchestrator = SimpleNamespace(
        reconcile_existing_faults=reconcile
    )
    monkeypatch.setattr(dldd_service, "TELEMETRY_FAILURE_LIMIT", 3)
    monkeypatch.setattr(dldd_service, "TELEMETRY_RETRY_INTERVAL", 0)

    assert service._reconcile_existing_faults_at_startup() is True
    assert len(calls) == 2
    assert "startup FAULT_INFO reconciliation failed (1/3)" in caplog.text

    service = _service(tmp_path)
    service.orchestrator = SimpleNamespace(
        reconcile_existing_faults=lambda: (_ for _ in ()).throw(
            RuntimeError("STATE_DB unavailable")
        )
    )
    waits = []
    service.stop_event = SimpleNamespace(
        wait=lambda timeout: waits.append(timeout) or True
    )
    monkeypatch.setattr(dldd_service, "TELEMETRY_FAILURE_LIMIT", 3)
    monkeypatch.setattr(dldd_service, "TELEMETRY_RETRY_INTERVAL", 0.25)

    assert service._reconcile_existing_faults_at_startup() is False
    assert waits == [0.25]

    service = _service(tmp_path)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("persistent STATE_DB read failure")

    service.orchestrator = SimpleNamespace(reconcile_existing_faults=fail)
    monkeypatch.setattr(dldd_service, "TELEMETRY_FAILURE_LIMIT", 2)
    monkeypatch.setattr(dldd_service, "TELEMETRY_RETRY_INTERVAL", 0)

    with pytest.raises(
        TelemetryUnavailable,
        match="STATE_DB fault reconciliation failed 2 consecutive times",
    ):
        service._reconcile_existing_faults_at_startup()

    assert len(calls) == 2
