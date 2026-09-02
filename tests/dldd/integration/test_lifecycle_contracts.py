from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from .conftest import FAULT_KEY, integration_rule_document


pytestmark = pytest.mark.dldd_integration


def test_no_rules_source_stops_cleanly_without_fatal_status(
    integration_environment_factory,
):
    environment = integration_environment_factory(document=None)
    running = environment.running().start()

    assert running.wait_stopped() is None
    assert running.service.stop_event.is_set()
    assert running.service.activation is None
    assert running.service.orchestrator is None
    assert not environment.row("DLDD_STATUS|process_state")
    assert not Path(environment.paths.state_file).exists()


def test_present_invalid_candidate_without_fallback_stays_fatal(
    integration_environment_factory,
):
    invalid = deepcopy(integration_rule_document())
    del invalid["signatures"][0]["signature"]["metadata"]["severity"]
    environment = integration_environment_factory(document=invalid)
    with environment.running() as running:
        status = environment.wait_for_status("BROKEN|FATAL")

        assert running.service.activation is None
        assert "no candidate produced a usable rules generation" in (
            running.service.fatal_reason
        )
        assert status["reason"] == running.service.fatal_reason


def test_invalid_candidate_falls_back_to_the_valid_active_generation(
    integration_environment_factory,
):
    invalid = deepcopy(integration_rule_document())
    del invalid["signatures"][0]["signature"]["metadata"]["severity"]
    environment = integration_environment_factory(document=invalid)
    active = Path(environment.paths.active)
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        yaml.safe_dump(integration_rule_document(), sort_keys=False),
        encoding="utf-8",
    )

    with environment.running() as running:
        status = environment.wait_for_status("OK")
        assert running.service.activation.source == "active"
        assert running.service.activation.fallback_used is True
        assert status["active_rules_source"] == "active"
        assert status["activation_fallback_used"] == "true"

        manifest = json.loads(
            Path(environment.paths.manifest).read_text(encoding="utf-8")
        )
        packaged, activated = manifest["activation_attempts"][-2:]
        assert packaged["activation_result"] == "REJECTED"
        assert activated["activation_result"] == "ACTIVATED"
        assert activated["fallback_used"] is True


def test_mixed_valid_and_broken_rules_activate_the_valid_work(
    integration_environment_factory,
):
    document = integration_rule_document()
    broken = deepcopy(document["signatures"][0])
    broken_signature = broken["signature"]
    broken_signature["metadata"].update(
        name="DLDD_INTEGRATION_BROKEN",
        id=9900099,
    )
    del broken_signature["metadata"]["severity"]
    document["signatures"].append(broken)

    environment = integration_environment_factory(document=document)
    with environment.running() as running:
        status = environment.wait_for_status("DEGRADED")

        activation = running.service.activation
        assert activation.validation_result == "DEGRADED"
        assert len(activation.payload.materialized_rules) == 1
        assert activation.broken_rules[0]["rule"] == (
            "DLDD_INTEGRATION_BROKEN"
        )
        assert json.loads(status["broken_rules"]) == list(
            activation.broken_rules
        )

        environment.source.set_value(20)
        active = environment.wait_for_row(FAULT_KEY, status="ACTIVE")
        assert active["rule"] == "DLDD_INTEGRATION_THRESHOLD"
        assert environment.state_db.keys("FAULT_INFO|*") == [FAULT_KEY]
