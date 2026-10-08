from __future__ import absolute_import

from types import SimpleNamespace

import pytest

from dldd.correlation import CorrelationEngine, SignatureExecution
from dldd.logic import parse_logic
from dldd.models import ValueConfig
from dldd.runtime import (
    CollectedValue,
    EvaluationResult,
    EvaluationResultType,
    FaultEvidenceEvent,
)


def signature(logic="1", lookback=0, events=None):
    events = events or (
        SimpleNamespace(id=1, match_count=1, match_period=0),
    )
    return SimpleNamespace(
        conditions=SimpleNamespace(
            events=events,
            logic_tree=parse_logic(logic),
            logic_lookback_time=lookback,
        )
    )


def item(event_id=1, key="event-1", component="SENSOR0"):
    return SimpleNamespace(
        rule_id=1000001,
        component_name=component,
        event_id=event_id,
        correlation_key=key,
        source_id="source:" + key,
        value_config=ValueConfig(type="float"),
    )


def test_signature_execution_preserves_ordered_owner_keys():
    execution = SignatureExecution(
        signature=signature(),
        component_name="SENSOR0",
        event_keys={1: ("owner-a", "owner-b"), 2: ("owner-c",)},
        plan_generation="generation",
    )

    assert execution.work_keys == ("owner-a", "owner-b", "owner-c")


def evidence(work, kind, timestamp, raw=1.0):
    return FaultEvidenceEvent(
        signature_id=work.rule_id,
        event_id=work.event_id,
        component_name=work.component_name,
        source_id=work.source_id,
        correlation_key=work.correlation_key,
        monitor_id="common",
        plan_generation="generation",
        work_state_generation=1,
        sequence=1,
        event_timestamp=timestamp,
        enqueue_timestamp=timestamp,
        result=EvaluationResult(
            kind,
            value=CollectedValue(raw, raw, work.value_config),
            evaluator_type="comparison",
            operator=">",
            expected=0,
            completed_at=timestamp,
        ),
    )


def test_registration_and_temporal_correlation_contract():
    rule = signature(
        events=(SimpleNamespace(id=1, match_count=1, match_period=100),)
    )
    work = item()
    engine = CorrelationEngine({})
    engine.register_work_item(rule, work, "generation")

    assert engine.consume(
        evidence(work, EvaluationResultType.MATCH, 10)
    ).active
    latest = item(key="latest")
    older_new_key = item(key="older-new-key")
    cleared = item(key="cleared")
    engine = CorrelationEngine({})
    for work in (latest, older_new_key, cleared):
        engine.register_work_item(rule, work, "generation")

    assert engine.consume(
        evidence(latest, EvaluationResultType.MATCH, 10, raw=10)
    ).active
    # A newly seen key may add an older match to the count window, but the
    # reported snapshot remains the most recent observation.
    decision = engine.consume(
        evidence(older_new_key, EvaluationResultType.MATCH, 5, raw=5)
    )
    assert decision.event_snapshots[0]["value_read"] == 10

    engine.consume(evidence(cleared, EvaluationResultType.NO_MATCH, 9))
    # An older match for a key already known clear must not resurrect it.
    decision = engine.consume(
        evidence(cleared, EvaluationResultType.MATCH, 8)
    )
    event_state = engine._events[(1000001, "SENSOR0", 1)]
    assert event_state.matching_keys["cleared"] is False
    assert decision.active
    events = (
        SimpleNamespace(id=1, match_count=1, match_period=0),
        SimpleNamespace(id=2, match_count=1, match_period=0),
    )
    engine = CorrelationEngine({})
    first = item(event_id=1, key="event-1")
    second = item(event_id=2, key="event-2")
    rule = signature("1 AND 2", lookback=5, events=events)
    engine.register_work_item(rule, first, "generation")
    engine.register_work_item(rule, second, "generation")

    assert not engine.consume(
        evidence(first, EvaluationResultType.MATCH, 1)
    ).active
    assert not engine.consume(
        evidence(second, EvaluationResultType.MATCH, 10)
    ).active
    work = item()
    rule = signature(
        events=(SimpleNamespace(id=1, match_count=2, match_period=5),)
    )
    engine = CorrelationEngine({})
    engine.register_work_item(rule, work, "generation")

    assert not engine.consume(
        evidence(work, EvaluationResultType.MATCH, 1)
    ).active
    assert not engine.consume(
        evidence(work, EvaluationResultType.MATCH, 10)
    ).active


def test_component_retirement_preserves_other_instance_state():
    engine = CorrelationEngine({})
    rule = signature()
    first = item(component="SENSOR0")
    second = item(component="SENSOR1")
    engine.register_work_item(rule, first, "generation")
    engine.register_work_item(rule, second, "generation")
    engine.consume(evidence(first, EvaluationResultType.MATCH, 1))
    engine.consume(evidence(second, EvaluationResultType.MATCH, 1))

    engine.retire(1000001, "SENSOR0")

    assert (1000001, "SENSOR0", 1) not in engine._events
    assert (1000001, "SENSOR1", 1) in engine._events


@pytest.mark.parametrize("logic,active_after_clear", [("1 AND 2", False),
                                                    ("1 OR 2", True)])
def test_restored_debounce_truth_respects_signature_logic(logic, active_after_clear):
    rule = signature(logic, lookback=10, events=(
        SimpleNamespace(id=1, match_count=2, match_period=10),
        SimpleNamespace(id=2, match_count=2, match_period=10),
    ))
    first, second = item(), item(event_id=2, key="event-2")
    engine = CorrelationEngine({})
    for work in (first, second):
        engine.register_work_item(rule, work, "generation")
    engine.restore_active(1000001, "SENSOR0", ({"id": 1}, {"id": 2}))

    decision = engine.consume(evidence(first, EvaluationResultType.MATCH, 100))
    assert decision.active and not decision.changed and not decision.confirmed
    decision = engine.consume(evidence(second, EvaluationResultType.NO_MATCH, 101))
    assert decision.active is active_after_clear
    assert not decision.confirmed
    decision = engine.consume(evidence(first, EvaluationResultType.MATCH, 102))
    assert decision.active is active_after_clear
    assert decision.confirmed is active_after_clear
    assert not engine.consume(
        evidence(first, EvaluationResultType.NO_MATCH, 103)
    ).active

    # A cleared lifetime and explicit retirement both lose the restored truth.
    assert not engine.consume(evidence(first, EvaluationResultType.MATCH, 104)).active
    engine.retire(1000001, "SENSOR0")
    assert engine._events == {}

    # A rule clear closes restoration for every branch of that lifetime.
    engine.restore_active(1000001, "SENSOR0", ({"id": 1}, {"id": 2}))
    assert engine.consume(evidence(
        first, EvaluationResultType.NO_MATCH, 200,
    )).active is active_after_clear
    for timestamp in (201, 202):
        assert engine.consume(evidence(
            first, EvaluationResultType.MATCH, timestamp,
        )).active is active_after_clear
    engine.consume(evidence(second, EvaluationResultType.MATCH, 203))
    decision = engine.consume(evidence(second, EvaluationResultType.MATCH, 204))
    assert decision.active and decision.confirmed


def test_restored_event_waits_for_all_owner_keys_before_clear():
    engine = CorrelationEngine({})
    first, second = item(key="owner-a"), item(key="owner-b")
    rule = signature(events=(SimpleNamespace(id=1, match_count=2, match_period=10),))
    for work in (first, second):
        engine.register_work_item(rule, work, "generation")
    engine.restore_active(1000001, "SENSOR0", ({"id": 1},))

    for work, kind, timestamp in (
        (first, EvaluationResultType.NO_MATCH, 100),
        (second, EvaluationResultType.COLLECTION_ERROR, 101),
        (second, EvaluationResultType.MATCH, 102),
    ):
        decision = engine.consume(evidence(work, kind, timestamp))
        assert decision.active and not decision.changed and not decision.confirmed
    decision = engine.consume(evidence(second, EvaluationResultType.NO_MATCH, 103))
    assert not decision.active and decision.changed

    # One owner can rebuild the positive count while another remains unknown.
    # Clearing only the sampled owner must not clear the retained assertion.
    engine.retire(1000001, "SENSOR0")
    engine.restore_active(1000001, "SENSOR0", ({"id": 1},))
    engine.consume(evidence(first, EvaluationResultType.MATCH, 100))
    engine.consume(evidence(second, EvaluationResultType.COLLECTION_ERROR, 101))
    assert engine.consume(evidence(first, EvaluationResultType.MATCH, 102)).confirmed
    decision = engine.consume(evidence(first, EvaluationResultType.NO_MATCH, 103))
    assert decision.active and not decision.changed and not decision.confirmed
    decision = engine.consume(evidence(second, EvaluationResultType.NO_MATCH, 104))
    assert not decision.active and decision.changed


def test_restored_truth_survives_lookback_while_debounce_is_pending():
    engine = CorrelationEngine({})
    first, second = item(), item(event_id=2, key="event-2")
    rule = signature("1 AND 2", lookback=10, events=(
        SimpleNamespace(id=1, match_count=2, match_period=10),
        SimpleNamespace(id=2, match_count=2, match_period=10),
    ))
    for work in (first, second):
        engine.register_work_item(rule, work, "generation")
    engine.restore_active(1000001, "SENSOR0", ({"id": 1}, {"id": 2}))

    for work, timestamp in ((first, 100), (second, 111), (first, 112),
                            (first, 113), (second, 114)):
        decision = engine.consume(evidence(work, EvaluationResultType.MATCH, timestamp))
        assert decision.active and not decision.changed
        assert decision.confirmed is (timestamp == 114)
    # Fresh confirmation ends restoration; normal history/lookback expiry resumes.
    assert not engine.consume(evidence(second, EvaluationResultType.MATCH, 125)).active


def test_restored_truth_does_not_confirm_expired_samples_with_unknown_owner():
    engine = CorrelationEngine({})
    first, unknown = item(key="owner-a"), item(key="owner-b")
    second = item(event_id=2, key="event-2")
    rule = signature("1 OR 2", lookback=10, events=(
        SimpleNamespace(id=1, match_count=2, match_period=100),
        SimpleNamespace(id=2, match_count=1, match_period=0),
    ))
    for work in (first, unknown, second):
        engine.register_work_item(rule, work, "generation")
    engine.restore_active(1000001, "SENSOR0", ({"id": 1},))
    engine.consume(evidence(first, EvaluationResultType.MATCH, 100))
    assert engine.consume(evidence(first, EvaluationResultType.MATCH, 101)).confirmed
    for work, timestamp in ((second, 120), (unknown, 121)):
        decision = engine.consume(evidence(work, EvaluationResultType.NO_MATCH, timestamp))
        assert decision.active and not decision.changed and not decision.confirmed
    assert not engine.consume(evidence(first, EvaluationResultType.NO_MATCH, 122)).active
