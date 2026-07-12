from __future__ import absolute_import

from types import SimpleNamespace

from dldd.correlation import CorrelationEngine
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
    engine = CorrelationEngine({})
    work = item()
    rule = signature()
    engine.register_work_item(rule, work, "generation")
    engine.register_work_item(rule, work, "generation")
    assert engine.executions[(1000001, "SENSOR0")].event_keys == {
        1: ("event-1",)
    }

    engine.unregister_work_item(item(component="OTHER"))
    unknown_event = item(event_id=2, key="event-2")
    assert engine.consume(
        evidence(unknown_event, EvaluationResultType.MATCH, 1)
    ) is None

    unavailable = engine.consume(
        evidence(work, EvaluationResultType.SOURCE_UNAVAILABLE, 2)
    )
    assert unavailable.active is False
    assert unavailable.changed is False
    assert unavailable.event_snapshots == ()


    rule = signature(
        events=(SimpleNamespace(id=1, match_count=1, match_period=100),)
    )
    work = item()
    engine = CorrelationEngine({})
    engine.register_work_item(rule, work, "generation")

    assert engine.consume(
        evidence(work, EvaluationResultType.MATCH, 10)
    ).active
    # This sample is old but still within the allowed lateness window. It may
    # contribute to match-count history, but cannot replace the key's state.
    assert engine.consume(
        evidence(work, EvaluationResultType.MATCH, 5)
    ).active
    assert engine.consume(
        evidence(work, EvaluationResultType.NO_MATCH, 4)
    ).active


    rule = signature(
        events=(SimpleNamespace(id=1, match_count=1, match_period=100),)
    )
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


def test_fault_value_projection_and_component_retirement_contract():
    assert CorrelationEngine._format_value(
        b"hello", ValueConfig(type="string", encoding="utf-8")
    ) == "hello"
    assert CorrelationEngine._format_value(
        b"\x80", ValueConfig(type="binary")
    ) == "0b10000000"
    assert CorrelationEngine._format_value(
        b"\xab", ValueConfig(type="hex")
    ) == "0xab"
    assert CorrelationEngine._format_value(
        b"\x00\xff", ValueConfig()
    ) == [0, 255]
    nested = (b"\x01", [b"\x02"], {"sample": b"\x03"})
    assert CorrelationEngine._format_value(nested, ValueConfig()) == [
        [1],
        [[2]],
        {"sample": [3]},
    ]


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
