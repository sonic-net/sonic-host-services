from __future__ import absolute_import

from queue import Queue
from types import SimpleNamespace

from dldd.planner import build_plans
from dldd.rule_status import build_rule_status_snapshot
from dldd.runtime import DSEWorkTemplate, MonitorExecutionPlan, MonitorWorkState
from dldd.validation import load_rules


def active_generation(result, broken_rules=()):
    return SimpleNamespace(payload=result, broken_rules=tuple(broken_rules))


def runtime_owner(work_items=(), broken_rules=(), faults=()):
    return SimpleNamespace(
        work_items={item.correlation_key: item for item in work_items},
        broken_rules={
            str(index): record for index, record in enumerate(broken_rules)
        },
        faults={str(index): fault for index, fault in enumerate(faults)},
    )


def loaded_rule_plan():
    result = load_rules("tests/dldd/fixtures/valid-redis-rule.json")
    bundle = build_plans(
        result.materialized_rules,
        "generation",
        {"redis": 60, "file": 60, "common": 60},
    )
    return result, bundle, next(iter(bundle.work_items.values()))


def test_rule_status_contract():
    assert build_rule_status_snapshot(None, None, ()) == ((), False)

    result, bundle, item = loaded_rule_plan()
    plan = bundle.monitor_plans["redis"]
    state = plan.state_by_key[item.correlation_key]
    state.next_sample_due = 15
    state.last_attempt_timestamp = 98.9
    state.last_success_timestamp = 97.8
    inactive = SimpleNamespace(
        rule_id=item.rule_id,
        component_name=item.component_name,
        status="INACTIVE",
    )

    rows, truncated = build_rule_status_snapshot(
        active_generation(result),
        runtime_owner((item,), faults=(inactive,)),
        (SimpleNamespace(plan=plan),),
        monotonic_now=10,
        wall_now=100,
    )

    assert not truncated
    assert rows[0]["active_faults"] == 0
    assert rows[0]["work_items"][0]["next_due"] == 105
    assert rows[0]["last_attempt"] == 98
    assert rows[0]["last_success"] == 97


    result, unused_bundle, item = loaded_rule_plan()

    rows, unused_truncated = build_rule_status_snapshot(
        active_generation(result),
        runtime_owner((item,)),
        (),
        monotonic_now=0,
        wall_now=100,
    )

    assert rows[0]["health"] == "DEGRADED"
    assert rows[0]["work_items"][0]["state"] == "UNKNOWN"
    assert rows[0]["work_items"][0]["monitor"] == ""
    assert rows[0]["work_items"][0]["failure_count"] == 0


    result, unused_bundle, item = loaded_rule_plan()
    template = DSEWorkTemplate(
        "template",
        item,
        result.materialized_rules[0].signature,
        SimpleNamespace(),
    )
    plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "generation",
        {},
        {},
        Queue(),
        templates_by_key={"template": template},
    )

    rows, unused_truncated = build_rule_status_snapshot(
        active_generation(result),
        runtime_owner(),
        (SimpleNamespace(plan=plan),),
        monotonic_now=0,
        wall_now=100,
    )

    assert rows[0]["health"] == "DEGRADED"
    assert rows[0]["reason"] == "dse_discovery_bootstrap"
    assert rows[0]["work_items_total"] == 0


    result, unused_bundle, item = loaded_rule_plan()
    template = DSEWorkTemplate(
        "template",
        item,
        result.materialized_rules[0].signature,
        SimpleNamespace(),
    )
    plan = MonitorExecutionPlan(
        "common",
        "common",
        60,
        "generation",
        {item.correlation_key: item},
        {},
        Queue(),
        templates_by_key={"template": template},
    )
    plan.expansion_state_by_key["template"].phase = "STABLE"
    plan.expansion_state_by_key["template"].last_error = (
        "authoritative inventory unavailable"
    )

    rows, unused_truncated = build_rule_status_snapshot(
        active_generation(result),
        runtime_owner((item,)),
        (SimpleNamespace(plan=plan),),
        monotonic_now=0,
        wall_now=100,
    )

    assert rows[0]["health"] == "DEGRADED"
    assert rows[0]["reason"] == "authoritative inventory unavailable"


    result, bundle, item = loaded_rule_plan()
    plan = bundle.monitor_plans["redis"]
    plan.state_by_key[item.correlation_key].state = MonitorWorkState.DEGRADED
    same_reason = {
        "rule_id": item.rule_id,
        "correlation_key": item.correlation_key,
        "reason": "source unavailable",
        "failure_count": 1,
    }
    no_identity = {
        "rule": "UNPARSEABLE",
        "reason": "schema root is invalid",
        "failure_count": 2,
    }
    # Candidate failures for a materialized rule are represented by its live
    # row and must not also create a duplicate ingestion-only row.
    materialized_candidate = {
        "rule_id": item.rule_id,
        "rule": item.rule_name,
        "reason": "already represented",
    }

    rows, unused_truncated = build_rule_status_snapshot(
        active_generation(
            result,
            broken_rules=(materialized_candidate, no_identity),
        ),
        runtime_owner(
            (item,),
            broken_rules=(
                same_reason,
                dict(same_reason),
                {"reason": "unmapped runtime diagnostic"},
            ),
        ),
        (SimpleNamespace(plan=plan),),
        monotonic_now=0,
        wall_now=100,
    )

    assert len(rows) == 2
    assert rows[0]["reason"] == "source unavailable"
    assert rows[1]["rule_id"] is None
    assert rows[1]["rule"] == "UNPARSEABLE"
    assert rows[1]["health"] == "BROKEN"
