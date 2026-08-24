from __future__ import absolute_import

from dataclasses import replace
import json
from pathlib import Path

import pytest

from dldd.models import (
    BrokenRule,
    RuleSet,
    ValidationIssue,
    ValidationResult,
    ValueConfig,
    frozen_mapping,
    to_mutable,
    value_config_contract_errors,
)
from dldd.validation import validate_document


FIXTURES = Path(__file__).parent / "fixtures"


def _validated_signature():
    document = json.loads((FIXTURES / "valid-redis-rule.json").read_text())
    return validate_document(document, materialize=False).ruleset.signatures[0]


def test_domain_container_freeze_mutable_conversion_and_event_aliases():
    assert dict(frozen_mapping(None)) == {}
    value = frozen_mapping({"nested": {"items": [1, {"enabled": True}]}})

    assert value["nested"]["items"] == (1, {"enabled": True})
    assert to_mutable(value) == {
        "nested": {"items": [1, {"enabled": True}]}
    }
    with pytest.raises(TypeError, match="expected a mapping"):
        frozen_mapping([("key", "value")])

    signature = _validated_signature()
    payload = to_mutable(signature)

    assert payload["schema_version"] == signature.schema_version
    assert isinstance(payload["conditions"]["events"], list)
    assert payload["metadata"]["product_ids"] == list(
        signature.metadata.product_ids
    )
    event = signature.conditions.events[0]
    assert event.source_type == event.type == "redis"


def test_value_config_and_validation_model_contracts():
    """Enforce value metadata, provenance, freezing, and activation state."""

    config = ValueConfig(type="float", unit="C", scaling=0.5)
    default = ValueConfig()
    unit_only = ValueConfig(unit="rule-units")

    assert ValueConfig.from_mapping(config) is config
    assert default.is_default
    assert not unit_only.is_default
    assert default.with_fallback(config) is config
    assert unit_only.with_fallback(config) is unit_only
    assert default.with_fallback(config.as_payload()) == config
    with pytest.raises(TypeError, match="must be a mapping"):
        ValueConfig.from_mapping("float")
    assert value_config_contract_errors(object()) == ("must be ValueConfig",)

    unsafe = object.__new__(ValueConfig)
    object.__setattr__(unsafe, "type", "vendor-private")
    object.__setattr__(unsafe, "unit", "C")
    object.__setattr__(unsafe, "scaling", "N/A")
    object.__setattr__(unsafe, "encoding", "N/A")
    with pytest.raises(ValueError, match="canonical value"):
        ValueConfig.from_mapping(unsafe)

    for changes, message in (
        ({"type": "vendor-private"}, "canonical value"),
        ({"unit": ""}, "unit must be a non-empty string"),
        ({"unit": 1}, "unit must be a non-empty string"),
        ({"scaling": True}, "scaling must be numeric"),
        ({"scaling": "2"}, "scaling must be numeric"),
        ({"encoding": ""}, "encoding must be a non-empty string"),
        ({"encoding": 1}, "encoding must be a non-empty string"),
    ):
        with pytest.raises(ValueError, match=message):
            ValueConfig(**changes)

    # Validation domain models enforce provenance and immutable activation data.
    signature = _validated_signature()

    with pytest.raises(ValueError, match="signature schema_version"):
        replace(signature, schema_version="")
    with pytest.raises(ValueError, match="signature schema_version"):
        replace(signature, schema_version=None)
    with pytest.raises(ValueError, match="ruleset schema_version"):
        RuleSet("", (signature,))
    with pytest.raises(ValueError, match="ruleset schema_version"):
        RuleSet(None, (signature,))
    with pytest.raises(ValueError, match="must match"):
        RuleSet("test-version", (signature,))

    issue = ValidationIssue("rule", "invalid", "bad rule", path="$.rule")
    broken = BrokenRule("RULE", 1000001, [issue])
    invalid = ValidationResult(
        schema_version=None,
        ruleset=None,
        materialized_rules=[],
        file_errors=[issue],
        broken_rules=[broken],
        source_lines={"$": 1},
    )

    assert str(issue) == "$.rule: bad rule (invalid)"
    assert broken.issues == (issue,)
    assert invalid.materialized_rules == ()
    assert invalid.file_errors == (issue,)
    assert invalid.broken_rules == (broken,)
    assert invalid.source_lines["$"] == 1
    assert invalid.usable_rules == ()
    assert invalid.file_valid is False
    assert invalid.activation_valid is False

    valid = ValidationResult(
        schema_version="test-version",
        ruleset=None,
        materialized_rules=[object()],
    )
    assert valid.file_valid is True
    assert valid.activation_valid is True
