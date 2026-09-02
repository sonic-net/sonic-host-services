from __future__ import absolute_import

from types import SimpleNamespace

import pytest

from dldd import preflight
from dldd.adapters import VendorAdapter
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import ValidationResult


class RuntimeHook(VendorHook):
    def collect(self, operation):
        return None

    def execute_action(self, action):
        return {}


class RejectingI2CHook(RuntimeHook):
    def validate_source(self, unused_operation):
        raise ValueError("logical bus is not mapped")


class RecordingAdapter(object):
    def __init__(self, error=None):
        self.validated = []
        self.error = error

    def validate(self, item):
        if self.error is not None:
            raise self.error
        self.validated.append(item.correlation_key)

    def get_value(self, unused_item):
        pytest.fail("activation preflight read a source value")

    def collect(self, unused_item):
        pytest.fail("activation preflight collected a source")


def _materialized_rule(rule_id, name, actions=(), queries=()):
    return SimpleNamespace(
        signature=SimpleNamespace(
            metadata=SimpleNamespace(
                id=rule_id,
                name=name,
                version="1.0.0",
            ),
            actions=SimpleNamespace(
                repair_actions=SimpleNamespace(
                    local_actions=(
                        SimpleNamespace(action_list=tuple(actions))
                        if actions
                        else None
                    )
                ),
                log_collection=(
                    SimpleNamespace(queries=tuple(queries))
                    if queries
                    else None
                ),
            ),
        )
    )


def _work_item(rule_id, source_type="redis", suffix="1"):
    return SimpleNamespace(
        rule_id=rule_id,
        source_type=source_type,
        correlation_key="{}:{}".format(rule_id, suffix),
    )


def test_activation_support_and_side_effect_free_rule_isolation(monkeypatch):
    hooks = VendorHookRegistry()
    hooks.register("vendor", RuntimeHook())
    operation = SimpleNamespace(
        type="vendor", executor=None, options={"hook": "vendor"}
    )
    query = SimpleNamespace(type="vendor", executor=None, options={})

    preflight.validate_runtime_operation_hooks(
        _materialized_rule(1000001, "RULE", (operation,), (query,)), hooks
    )

    extensions = SimpleNamespace(
        vendor_hooks=hooks,
        dse_registry=SimpleNamespace(source_types=("platform_sensor",)),
    )
    adapters = preflight.build_adapter_registry(extensions)

    assert isinstance(adapters["platform_sensor"], VendorAdapter)
    assert adapters["platform_sensor"].source_type == "platform_sensor"

    bad_operation = SimpleNamespace(
        type="missing-hook", executor=None, options={}
    )
    i2c_operation = SimpleNamespace(
        type="i2c", path={"bus": "logical"}, executor=None, options={}
    )
    broken = _materialized_rule(1000002, "BROKEN", (bad_operation,))
    broken_i2c = _materialized_rule(1000004, "BROKEN_I2C", (i2c_operation,))
    missing_adapter = _materialized_rule(1000001, "NO_ADAPTER")
    valid = _materialized_rule(1000003, "VALID")
    validation = ValidationResult(
        schema_version="0.0.1",
        ruleset=None,
        materialized_rules=(broken, broken_i2c, missing_adapter, valid),
        source_lines={"$.signatures": 1},
    )
    items = (
        _work_item(1000002),
        _work_item(1000004),
        _work_item(1000001, "not-installed"),
        _work_item(1000003),
    )
    plan = SimpleNamespace(
        work_items={item.correlation_key: item for item in items},
        templates={},
    )
    filtered_plan = SimpleNamespace(
        work_items={items[-1].correlation_key: items[-1]}, templates={}
    )
    adapter = RecordingAdapter()
    runtime_hooks = VendorHookRegistry()
    runtime_hooks.register("i2c", RejectingI2CHook())
    extensions = SimpleNamespace(vendor_hooks=runtime_hooks)
    plans = iter((plan, filtered_plan))
    monkeypatch.setattr(preflight, "build_plans", lambda *args: next(plans))
    monkeypatch.setattr(
        preflight,
        "build_adapter_registry",
        lambda unused_extensions: {"redis": adapter},
    )

    result = preflight.preflight_activation(
        validation,
        extensions,
        {"redis": 60, "file": 60, "common": 60},
    )

    assert result.validation.materialized_rules == (valid,)
    assert result.plan is filtered_plan
    assert result.adapters["redis"] is adapter
    assert result.invalid_rule_ids == frozenset((1000001, 1000002, 1000004))
    assert adapter.validated == ["1000003:1"]
    assert [failure.rule_name for failure in result.failures] == [
        "BROKEN",
        "BROKEN_I2C",
        "NO_ADAPTER",
    ]
    assert [failure.correlation_key for failure in result.failures] == [
        "rule:1000002",
        "rule:1000004",
        "1000001:1",
    ]
    assert "not registered" in result.failures[0].message
    assert "not mapped" in result.failures[1].message
    assert "no adapter is registered" in result.failures[2].message
    assert [item.rule_id for item in result.validation.broken_rules] == [
        1000002,
        1000004,
        1000001,
    ]


def test_preflight_validates_each_dse_common_item_once(monkeypatch):
    rule = _materialized_rule(1000001, "DSE_RULE")
    static = _work_item(1000001, suffix="static")
    common = _work_item(1000001, suffix="common")
    plan = SimpleNamespace(
        work_items={static.correlation_key: static},
        templates={
            "template": SimpleNamespace(common_items=(static, common))
        },
    )
    adapter = RecordingAdapter()
    extensions = SimpleNamespace(vendor_hooks=VendorHookRegistry())
    monkeypatch.setattr(preflight, "build_plans", lambda *args: plan)
    monkeypatch.setattr(
        preflight,
        "build_adapter_registry",
        lambda unused_extensions: {"redis": adapter},
    )

    result = preflight.preflight_activation(
        SimpleNamespace(materialized_rules=(rule,)),
        extensions,
        {"redis": 60, "file": 60, "common": 60},
    )

    assert result.failures == ()
    assert adapter.validated == ["1000001:static", "1000001:common"]

    monkeypatch.setattr(
        preflight,
        "build_adapter_registry",
        lambda unused_extensions: {
            "redis": RecordingAdapter(RuntimeError("adapter bug"))
        },
    )
    with pytest.raises(RuntimeError, match="adapter bug"):
        preflight.preflight_activation(
            SimpleNamespace(materialized_rules=(rule,)),
            extensions,
            {"redis": 60, "file": 60, "common": 60},
        )
