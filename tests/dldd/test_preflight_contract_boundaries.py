from __future__ import absolute_import

from types import SimpleNamespace

from dldd import preflight
from dldd.adapters import VendorAdapter
from dldd.hooks import VendorHook, VendorHookRegistry


class RuntimeHook(VendorHook):
    def collect(self, operation):
        return None

    def execute_action(self, action):
        return {}


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


def test_activation_support_and_localized_preflight_failure_contract(monkeypatch):
    """Build advertised adapters and localize operation/adapter failures."""

    hooks = VendorHookRegistry()
    hooks.register("vendor", RuntimeHook())
    operations = (
        SimpleNamespace(
            type="i2c", path={"bus": "1"}, executor=None, options={}
        ),
        SimpleNamespace(
            type="deferred", path={}, executor=lambda operation: None, options={}
        ),
        SimpleNamespace(type="cli", path={}, executor=None, options={}),
        SimpleNamespace(
            type="vendor", path={}, executor=None, options={"hook": "vendor"}
        ),
    )
    queries = (
        SimpleNamespace(type="cli", executor=None, options={}),
        SimpleNamespace(type="deferred", executor=lambda query: None, options={}),
        SimpleNamespace(type="vendor", executor=None, options={}),
    )

    preflight.validate_runtime_operation_hooks(
        _materialized_rule(1000001, "RULE", operations, queries), hooks
    )

    extensions = SimpleNamespace(
        vendor_hooks=hooks,
        dse_registry=SimpleNamespace(source_types=("platform_sensor",)),
    )

    adapters = preflight.build_adapter_registry(extensions)

    assert isinstance(adapters["platform_sensor"], VendorAdapter)
    assert adapters["platform_sensor"].source_type == "platform_sensor"

    # Rejected operations skip source validation and remain rule-local.
    bad_operation = SimpleNamespace(
        type="missing-hook", executor=None, options={}
    )
    broken = _materialized_rule(1000001, "BROKEN", (bad_operation,))
    missing_adapter = _materialized_rule(1000002, "NO_ADAPTER")
    validation = SimpleNamespace(materialized_rules=(broken, missing_adapter))
    bad_item = SimpleNamespace(
        rule_id=1000001,
        source_type="redis",
        correlation_key="1000001:1",
    )
    missing_item = SimpleNamespace(
        rule_id=1000002,
        source_type="not-installed",
        correlation_key="1000002:1",
    )
    plan = SimpleNamespace(
        work_items={
            bad_item.correlation_key: bad_item,
            missing_item.correlation_key: missing_item,
        },
        templates={},
    )

    class MustNotValidate(object):
        def validate(self, unused_item):
            raise AssertionError("operation-rejected rule reached its adapter")

    extensions = SimpleNamespace(vendor_hooks=VendorHookRegistry())
    monkeypatch.setattr(preflight, "build_plans", lambda *args: plan)
    monkeypatch.setattr(
        preflight,
        "build_adapter_registry",
        lambda unused_extensions: {"redis": MustNotValidate()},
    )

    result = preflight.preflight_activation(
        validation,
        extensions,
        {"redis": 60, "file": 60, "common": 60},
    )

    assert result.invalid_rule_ids == frozenset((1000001, 1000002))
    assert [failure.rule_name for failure in result.failures] == [
        "BROKEN",
        "NO_ADAPTER",
    ]
    assert result.failures[0].correlation_key == "rule:1000001"
    assert result.failures[1].correlation_key == "1000002:1"
    assert "not registered" in result.failures[0].message
    assert "no adapter is registered" in result.failures[1].message


def test_preflight_validates_dse_template_common_items_once(monkeypatch):
    rule = _materialized_rule(1000001, "DSE_RULE")
    validation = SimpleNamespace(materialized_rules=(rule,))
    static = SimpleNamespace(
        rule_id=1000001,
        source_type="redis",
        correlation_key="1000001:static",
    )
    template_common = SimpleNamespace(
        rule_id=1000001,
        source_type="redis",
        correlation_key="1000001:common",
    )
    plan = SimpleNamespace(
        work_items={static.correlation_key: static},
        templates={
            "template": SimpleNamespace(
                common_items=(static, template_common)
            )
        },
    )
    validated = []

    class RecordingAdapter(object):
        def validate(self, item):
            validated.append(item.correlation_key)

    extensions = SimpleNamespace(vendor_hooks=VendorHookRegistry())
    monkeypatch.setattr(preflight, "build_plans", lambda *args: plan)
    monkeypatch.setattr(
        preflight,
        "build_adapter_registry",
        lambda unused_extensions: {"redis": RecordingAdapter()},
    )

    result = preflight.preflight_activation(
        validation,
        extensions,
        {"redis": 60, "file": 60, "common": 60},
    )

    assert result.failures == ()
    assert validated == ["1000001:static", "1000001:common"]
