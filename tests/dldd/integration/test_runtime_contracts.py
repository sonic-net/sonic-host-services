from __future__ import absolute_import

import json

import pytest

from .conftest import FAULT_KEY, eventually, integration_rule_document


pytestmark = pytest.mark.dldd_integration


def test_real_service_action_waits_then_rechecks_before_publication(
    integration_environment_factory,
):
    document = integration_rule_document()
    signature = document["signatures"][0]["signature"]
    signature["conditions"]["events"][0]["event"]["async"] = True
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
        assert json.loads(active["actions_taken"])[0]["status"] == "SUCCESS"
        assert json.loads(active["local_action_state"])["state"] == "COMPLETED"
        assert len(source.read_calls) >= 3
