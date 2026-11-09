from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path
from threading import Event

import pytest
import yaml

from dldd.dse import (
    DSEEvaluationHandle,
    DSEExpansionResult,
    DSERegistry,
    DSESourceHandle,
)
from dldd.runtime import MonitorWorkState
from dldd.watcher import RulesWatcher

from .conftest import (
    CONFIG_VALUES,
    DSE_FAULT_KEY,
    BlockingConfigDB,
    ControlledDSEHook,
    IntegrationService,
    dse_integration_rule_document,
    eventually,
)


pytestmark = pytest.mark.dldd_integration

BROKEN_INSTANCE = "DSE_SENSOR0"
HEALTHY_INSTANCE = "DSE_SENSOR1"
FAULT_IDENTITY = (9900002, BROKEN_INSTANCE)
UNRELATED_RULE_NAME = "DLDD_UNRELATED_DSE_INTEGRATION"


class FailingComparatorHook(ControlledDSEHook):
    """Fail one live comparator without changing its resolved DSE contract."""

    def __init__(self):
        super(FailingComparatorHook, self).__init__()
        self.comparator_broken = False

    def resolve_evaluation(self, reference, context):
        handle = super(FailingComparatorHook, self).resolve_evaluation(
            reference, context
        )

        def get_comparator(invocation):
            with self._lock:
                result = handle.get_comparator(invocation)
                if (
                    self.comparator_broken
                    and invocation.binding.instance == BROKEN_INSTANCE
                ):
                    raise ValueError("synthetic invalid dynamic comparator")
                return result

        return DSEEvaluationHandle(reference, get_comparator)

    def set_comparator_broken(self, broken):
        with self._lock:
            self.comparator_broken = broken

    def callback_counts(self, instance):
        with self._lock:
            return (
                self.source_calls.count(instance),
                self.comparator_calls.count(instance),
            )

    def restore_instance(self, instance, value):
        with self._lock:
            self._values[instance] = str(value)
            self._thresholds[instance] = 10.0
            self._visible_instances.add(instance)


class SyntheticServiceCrash(RuntimeError):
    pass


class UnavailableSiblingExpansionHook(FailingComparatorHook):
    """Keep a second rule's inventory unavailable independently of rule A."""

    def __init__(self):
        super(UnavailableSiblingExpansionHook, self).__init__()
        self.unrelated_expansion_unavailable = False
        self.failed_expansion_calls = 0

    def resolve_source(self, reference, context):
        handle = super(UnavailableSiblingExpansionHook, self).resolve_source(
            reference, context
        )
        if context.rule_name != UNRELATED_RULE_NAME:
            return handle

        def expand(unused_context):
            with self._lock:
                if self.unrelated_expansion_unavailable:
                    self.failed_expansion_calls += 1
                    raise RuntimeError("synthetic unrelated inventory outage")
                return DSEExpansionResult(())

        return DSESourceHandle(reference, expand, handle.get_value)

    def fail_unrelated_expansion(self):
        with self._lock:
            self.unrelated_expansion_unavailable = True


class CrashableIntegrationService(IntegrationService):
    """Abort the real run loop so its real finally block saves unclean state."""

    def __init__(self, *args, **kwargs):
        self.crash_requested = Event()
        super(CrashableIntegrationService, self).__init__(*args, **kwargs)

    def _supervise_monitors(self):
        if self.crash_requested.is_set():
            raise SyntheticServiceCrash("synthetic unclean service exit")
        super(CrashableIntegrationService, self)._supervise_monitors()


@pytest.fixture
def broken_dse_environment(integration_environment_factory):
    hook = FailingComparatorHook()
    return integration_environment_factory(
        document=dse_integration_rule_document(),
        source=hook,
        dse_registry=DSERegistry(hook=hook),
    )


def _persisted_state(environment):
    return json.loads(
        Path(environment.paths.state_file).read_text(encoding="utf-8")
    )


def _monitor_state(service, key):
    for monitor in tuple(service.monitors):
        unused_items, states = monitor.plan.runtime_snapshot()
        if key in states:
            return states[key].state
    return None


def _crash_after_dynamic_comparator_breaks(environment):
    hook = environment.source
    hook.set_value(BROKEN_INSTANCE, 20)
    stop_event = Event()
    service = CrashableIntegrationService(
        hook,
        paths=environment.paths,
        config_db=BlockingConfigDB(stop_event, CONFIG_VALUES),
        state_db=environment.state_db,
        extensions=environment.new_service().extensions,
        stop_event=stop_event,
    )
    running = environment.running(service).start()
    try:
        active = environment.wait_for_row(DSE_FAULT_KEY, status="ACTIVE")
        orchestrator = service.orchestrator
        assert orchestrator is not None
        hook.set_comparator_broken(True)
        broken = eventually(
            lambda: next(
                (
                    record
                    for record in orchestrator.broken_rules.values()
                    if record.get("state") == "BROKEN"
                ),
                None,
            )
        )
        key = broken["correlation_key"]
        assert "synthetic invalid dynamic comparator" in broken["reason"]
        assert broken["failure_count"] >= 1
        eventually(
            lambda: _monitor_state(service, key) == MonitorWorkState.BROKEN
        )
        service.crash_requested.set()
        assert isinstance(running.wait_stopped(), SyntheticServiceCrash)
    finally:
        if running.thread.is_alive():
            running.stop()

    persisted = _persisted_state(environment)
    assert persisted["clean_shutdown"] is False
    assert persisted["active_rules_checksum"] == active["active_rules_checksum"]
    assert [
        record["correlation_key"] for record in persisted["broken_rules"]
    ] == [key]
    assert all(not monitor.is_alive() for monitor in service.monitors)
    hook.set_comparator_broken(False)
    hook.set_value(BROKEN_INSTANCE, 5)
    return active, persisted["broken_rules"][0]


def test_unclean_restart_preserves_dynamic_broken_until_clean_restart(
    broken_dse_environment,
):
    environment = broken_dse_environment
    hook = environment.source
    active, broken = _crash_after_dynamic_comparator_breaks(environment)
    key = broken["correlation_key"]
    broken_calls = hook.callback_counts(BROKEN_INSTANCE)
    healthy_calls = hook.callback_counts(HEALTHY_INSTANCE)

    with environment.running() as restarted:
        service = restarted.service
        eventually(
            lambda: _monitor_state(service, key) == MonitorWorkState.BROKEN
        )
        retained = environment.wait_for_row(
            DSE_FAULT_KEY, status="ACTIVE", source_stale="true"
        )
        assert retained["origin_time"] == active["origin_time"]
        assert retained["occurrences"] == active["occurrences"]
        assert service.orchestrator.broken_rules[key] == broken
        assert service.activation.checksum == active["active_rules_checksum"]

        # Observe two actual periodic reconciliation cycles with valid, low
        # sensor data available. Neither cycle may invoke the broken child.
        deadline = eventually(
            lambda: service.orchestrator.next_active_recheck.get(FAULT_IDENTITY)
        )
        for unused_cycle in range(2):
            deadline = eventually(
                lambda: (
                    service.orchestrator.next_active_recheck.get(FAULT_IDENTITY)
                    if service.orchestrator.next_active_recheck.get(
                        FAULT_IDENTITY, 0
                    ) > deadline
                    else None
                )
            )
        assert hook.callback_counts(BROKEN_INSTANCE) == broken_calls
        assert all(
            after > before
            for after, before in zip(
                hook.callback_counts(HEALTHY_INSTANCE), healthy_calls
            )
        )
        assert service.orchestrator.broken_rules[key] == broken
        retained = environment.row(DSE_FAULT_KEY)
        assert retained["status"] == "ACTIVE"
        assert retained["source_stale"] == "true"
        assert retained["origin_time"] == active["origin_time"]
        assert retained["occurrences"] == active["occurrences"]

    assert _persisted_state(environment)["clean_shutdown"] is True
    with environment.running() as restarted:
        inactive = environment.wait_for_row(DSE_FAULT_KEY, status="INACTIVE")
        assert inactive["origin_time"] == active["origin_time"]
        assert inactive["occurrences"] == active["occurrences"]
        assert not inactive.get("source_stale")
        assert not restarted.service.orchestrator.broken_rules
        assert all(
            after > before
            for after, before in zip(
                hook.callback_counts(BROKEN_INSTANCE), broken_calls
            )
        )


def test_unclean_restart_retires_a_missing_broken_dynamic_instance(
    broken_dse_environment,
):
    environment = broken_dse_environment
    hook = environment.source
    active, broken = _crash_after_dynamic_comparator_breaks(environment)
    hook.remove(BROKEN_INSTANCE)
    broken_calls = hook.callback_counts(BROKEN_INSTANCE)
    healthy_calls = hook.callback_counts(HEALTHY_INSTANCE)

    with environment.running() as restarted:
        inactive = environment.wait_for_row(DSE_FAULT_KEY, status="INACTIVE")
        assert "DSE discovery" in inactive["reason"]
        assert inactive["origin_time"] == active["origin_time"]
        assert inactive["occurrences"] == active["occurrences"]
        assert not inactive.get("source_stale")
        assert environment.state_db.ttls[DSE_FAULT_KEY] == 3600
        eventually(lambda: not restarted.service.orchestrator.broken_rules)
        assert _monitor_state(restarted.service, broken["correlation_key"]) is None
        assert hook.callback_counts(BROKEN_INSTANCE) == broken_calls
        eventually(
            lambda: all(
                after > before
                for after, before in zip(
                    hook.callback_counts(HEALTHY_INSTANCE), healthy_calls
                )
            )
        )
        eventually(lambda: not _persisted_state(environment)["broken_rules"])

        # An absent startup identity has retired its old BROKEN state. Its
        # later discovery creates ordinary work that can sample and fault.
        hook.restore_instance(BROKEN_INSTANCE, 5)
        eventually(
            lambda: all(
                after > before
                for after, before in zip(
                    hook.callback_counts(BROKEN_INSTANCE), broken_calls
                )
            )
        )
        assert broken["correlation_key"] not in (
            restarted.service.orchestrator.broken_rules
        )
        hook.set_value(BROKEN_INSTANCE, 20)
        reactivated = environment.wait_for_row(DSE_FAULT_KEY, status="ACTIVE")
        assert int(reactivated["occurrences"]) == int(active["occurrences"]) + 1
        assert not reactivated.get("source_stale")


def test_new_rules_checksum_does_not_restore_old_dynamic_broken_state(
    broken_dse_environment,
):
    environment = broken_dse_environment
    hook = environment.source
    active, unused_broken = _crash_after_dynamic_comparator_breaks(environment)
    broken_calls = hook.callback_counts(BROKEN_INSTANCE)
    document = dse_integration_rule_document()
    document["signatures"][0]["signature"]["metadata"]["description"] = (
        "Updated generation for the dynamic restart integration test."
    )
    inbox = Path(environment.paths.inbox)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    restart_requested = Event()
    watcher = RulesWatcher(
        environment.paths.inbox,
        environment.paths.lock,
        environment.paths.watcher_state,
        settle_time=0,
        restart=restart_requested.set,
    )
    assert watcher.check_once() is False
    assert watcher.check_once() is True
    assert restart_requested.is_set()

    with environment.running() as restarted:
        inactive = environment.wait_for_row(DSE_FAULT_KEY, status="INACTIVE")
        assert restarted.service.activation.source == "inbox"
        assert restarted.service.activation.checksum != (
            active["active_rules_checksum"]
        )
        assert "stale rule/source" in inactive["reason"]
        assert not restarted.service.orchestrator.broken_rules
        eventually(
            lambda: all(
                after > before
                for after, before in zip(
                    hook.callback_counts(BROKEN_INSTANCE), broken_calls
                )
            )
        )
        eventually(
            lambda: (
                _persisted_state(environment)["active_rules_checksum"]
                == restarted.service.activation.checksum
                and not _persisted_state(environment)["broken_rules"]
            )
        )


def test_unrelated_inventory_failure_does_not_preserve_retired_broken_child(
    integration_environment_factory,
):
    document = dse_integration_rule_document()
    unrelated = deepcopy(document["signatures"][0])
    signature = unrelated["signature"]
    signature["metadata"].update(name=UNRELATED_RULE_NAME, id=9900003)
    event = signature["conditions"]["events"][0]["event"]
    event["path"] = "{unrelated*}:{get_value()}"
    event["evaluation"]["value"] = (
        "{unrelated*}:{get_high_threshold()}"
    )
    document["signatures"].append(unrelated)
    hook = UnavailableSiblingExpansionHook()
    environment = integration_environment_factory(
        document=document,
        source=hook,
        dse_registry=DSERegistry(hook=hook),
    )
    active, broken = _crash_after_dynamic_comparator_breaks(environment)
    hook.remove(BROKEN_INSTANCE)
    hook.fail_unrelated_expansion()
    broken_calls = hook.callback_counts(BROKEN_INSTANCE)

    with environment.running() as restarted:
        inactive = environment.wait_for_row(DSE_FAULT_KEY, status="INACTIVE")
        assert "DSE discovery" in inactive["reason"]
        assert inactive["origin_time"] == active["origin_time"]
        eventually(lambda: hook.failed_expansion_calls >= 1)
        orchestrator = restarted.service.orchestrator
        assert orchestrator is not None
        eventually(lambda: not orchestrator.broken_rules)
        assert _monitor_state(restarted.service, broken["correlation_key"]) is None
        assert hook.callback_counts(BROKEN_INSTANCE) == broken_calls
        eventually(lambda: not _persisted_state(environment)["broken_rules"])

        # Rule A has completed its own inventory. Rule B's repeated failures
        # must not leave rule A's retired startup suspension waiting on B.
        hook.restore_instance(BROKEN_INSTANCE, 5)
        eventually(
            lambda: all(
                after > before
                for after, before in zip(
                    hook.callback_counts(BROKEN_INSTANCE), broken_calls
                )
            )
        )
        eventually(lambda: hook.failed_expansion_calls >= 2)
        assert hook.unrelated_expansion_unavailable
        assert broken["correlation_key"] not in (
            orchestrator.broken_rules
        )
        assert _monitor_state(restarted.service, broken["correlation_key"]) != (
            MonitorWorkState.BROKEN
        )
