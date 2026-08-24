from __future__ import absolute_import

from copy import deepcopy
from dataclasses import replace
import json
from queue import Queue
import time

import pytest

from dldd.adapters import RedisAdapter
from dldd.correlation import CorrelationEngine
from dldd.monitor import (
    MonitorThread,
    command_for_plan,
)
from dldd.runtime import (
    MonitorCommandType,
    MonitorWorkState,
)
from dldd.validation import validate_document
from dldd.planner import build_plans

from .conftest import (
    FAULT_KEY,
    eventually,
    integration_rule_document,
)


pytestmark = pytest.mark.dldd_integration


def test_real_service_action_waits_then_rechecks_async_event_before_publication(
    integration_environment_factory,
):
    document = integration_rule_document()
    signature = document["signatures"][0]["signature"]
    event = signature["conditions"]["events"][0]["event"]
    event["async"] = True
    signature["actions"]["repair_actions"]["local_actions"] = {
        "wait_period": 1,
        "action_list": [
            {"action": {"type": "cli", "argv": ["/usr/bin/true"]}}
        ],
    }
    environment = integration_environment_factory(document=document)
    source = environment.source
    with environment.running() as running:
        environment.wait_for_status("OK")
        source.set_value(20)
        pending = eventually(
            lambda: (
                next(iter(running.service.orchestrator.pending.values()), None)
                if running.service.orchestrator is not None
                else None
            )
        )
        assert pending.phase in ("ACTIONS", "WAITING_FOR_RECHECK")
        assert not environment.row(FAULT_KEY)

        active = environment.wait_for_row(FAULT_KEY, status="ACTIVE")
        actions = json.loads(active["actions_taken"])
        assert actions and actions[0]["status"] == "SUCCESS"
        assert json.loads(active["local_action_state"])["state"] == "COMPLETED"
        # Startup/normal detection plus the post-wait RECHECK_ONCE must all
        # reach the same live source callback before publication.
        assert len(source.read_calls) >= 3


class _Clock(object):
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@pytest.mark.parametrize("lookback,expected_active", ((0, True), (60, False)))
def test_monitor_and_correlation_apply_current_truth_and_positive_lookback(
    lookback, expected_active
):
    document = integration_rule_document()
    conditions = document["signatures"][0]["signature"]["conditions"]
    first = conditions["events"][0]["event"]
    second_wrapper = deepcopy(conditions["events"][0])
    second = second_wrapper["event"]
    first["id"] = 1
    first["path"]["key"] = "DLDD_TEST_SENSOR|A"
    first["sampling_interval"] = 1
    second["id"] = 2
    second["path"]["key"] = "DLDD_TEST_SENSOR|B"
    second["sampling_interval"] = 1000
    conditions["events"].append(second_wrapper)
    conditions["logic"] = "1 AND 2"
    conditions["logic_lookback_time"] = lookback
    validation = validate_document(document)
    assert validation.activation_valid
    bundle = build_plans(
        validation.materialized_rules,
        "integration-correlation",
        {"redis": 60, "file": 60, "common": 60},
    )
    values = {
        "DLDD_TEST_SENSOR|A": "5",
        "DLDD_TEST_SENSOR|B": "20",
    }

    def read(unused_database, unused_table, key):
        return {"value": values[key]}

    clock = _Clock()
    evidence = Queue()
    transport = RedisAdapter(read)

    class TimestampedAdapter(object):
        def collect(self, item):
            return replace(
                transport.collect(item),
                completed_at=1000.0 + clock.value,
                source_timestamp=1000.0 + clock.value,
            )

    monitor = MonitorThread(
        bundle.monitor_plans["redis"],
        {"redis": TimestampedAdapter()},
        evidence,
        clock=clock,
        wall_clock=lambda: 1000.0 + clock.value,
    )
    correlation = CorrelationEngine(bundle.signatures)

    monitor.run_once()
    old_match = evidence.get_nowait()
    first_decision = correlation.consume(old_match)
    assert first_decision is not None and not first_decision.active
    assert monitor.apply_command(
        command_for_plan(
            monitor.plan,
            old_match.correlation_key,
            MonitorCommandType.RESUME,
            MonitorWorkState.READY,
            "integration evidence accepted",
            evidence=old_match,
        )
    )

    values["DLDD_TEST_SENSOR|A"] = "20"
    clock.value = 300.0
    monitor.run_once()
    new_match = evidence.get_nowait()
    assert new_match.event_id == 1
    decision = correlation.consume(new_match)

    assert decision is not None
    assert decision.active is expected_active

    if lookback == 0:
        assert monitor.apply_command(
            command_for_plan(
                monitor.plan,
                new_match.correlation_key,
                MonitorCommandType.RESUME,
                MonitorWorkState.READY,
                "integration evidence accepted",
                evidence=new_match,
            )
        )
        values["DLDD_TEST_SENSOR|B"] = "5"
        clock.value = 1001.0
        monitor.run_once()
        decisions = []
        while not evidence.empty():
            event = evidence.get_nowait()
            result = correlation.consume(event)
            if result is not None:
                decisions.append(result)
            assert monitor.apply_command(
                command_for_plan(
                    monitor.plan,
                    event.correlation_key,
                    MonitorCommandType.RESUME,
                    MonitorWorkState.READY,
                    "integration evidence accepted",
                    evidence=event,
                )
            )

        assert decisions
        assert decisions[-1].active is False


def test_live_config_update_changes_inherited_cadence_without_postponing_due_work(
    integration_environment_factory,
):
    document = integration_rule_document()
    document["signatures"][0]["signature"]["conditions"]["events"][0][
        "event"
    ].pop("sampling_interval", None)
    initial_config = {
        "redis_monitor_polling_interval": "10",
        "file_monitor_polling_interval": "11",
        "common_monitor_polling_interval": "12",
        "source_unavailable_grace_period": "0",
    }
    environment = integration_environment_factory(
        document=document, config_values=initial_config
    )
    with environment.running() as running:
        plan = eventually(
            lambda: (
                running.service.monitors[0].plan
                if running.service.monitors
                else None
            )
        )
        item = next(iter(plan.items_by_key.values()))
        state = plan.state_by_key[item.correlation_key]
        before = eventually(lambda: state.next_sample_due)

        running.service._apply_config(
            {
                "redis_monitor_polling_interval": "2",
                "file_monitor_polling_interval": "7",
                "common_monitor_polling_interval": "8",
            }
        )
        eventually(lambda: plan.polling_intervals["redis"] == 2.0)
        shortened = state.next_sample_due
        assert shortened <= before
        assert shortened <= time.monotonic() + 2.1

        running.service._apply_config(
            {
                "redis_monitor_polling_interval": "20",
                "file_monitor_polling_interval": "21",
                "common_monitor_polling_interval": "22",
            }
        )
        eventually(lambda: plan.polling_intervals["redis"] == 20.0)
        assert state.next_sample_due <= shortened
