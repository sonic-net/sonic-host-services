from __future__ import absolute_import

import json
import os

import pytest

from dldd.lifecycle import (
    BrokenRuleStateStore,
    CandidateValidation,
    RuleGenerationManager,
    RulePaths,
    sha256_file,
)
from dldd.watcher import RulesWatcher


def paths(tmp_path):
    platform = tmp_path / "platform"
    platform.mkdir()
    return RulePaths(
        str(platform),
        inbox=str(tmp_path / "inbox" / "dld_rules.yaml"),
        rules_dir=str(tmp_path / "rules"),
        state_file=str(tmp_path / "state.json"),
    )


def test_watcher_state_is_outside_remotely_writable_inbox(tmp_path):
    rule_paths = paths(tmp_path)

    assert os.path.dirname(rule_paths.watcher_state) == rule_paths.rules_dir
    assert os.path.dirname(rule_paths.watcher_state) != os.path.dirname(
        rule_paths.inbox
    )


def validator(path, _dse):
    content = open(path).read()
    valid = content != "invalid"
    return CandidateValidation(valid, 1 if valid else 0, "0.0.1", errors=() if valid else ("invalid",), payload=content)


def test_packaged_generation_is_atomically_promoted(tmp_path):
    rule_paths = paths(tmp_path)
    with open(rule_paths.packaged, "w") as stream:
        stream.write("valid")
    result = RuleGenerationManager(
        rule_paths,
        validator,
        "platform-v1",
        clock=lambda: 1234.9,
    ).activate()
    assert result.source == "packaged"
    assert open(rule_paths.active).read() == "valid"
    assert result.checksum == sha256_file(rule_paths.active)
    manifest = json.load(open(rule_paths.manifest))
    assert manifest["active_checksum"] == result.checksum
    assert manifest["activated_at"] == 1234
    assert manifest["last_activation"]["at"] == 1234
    assert manifest["last_attempt"]["at"] == 1234


def test_malformed_activation_manifest_is_treated_as_empty(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.packaged, "w") as stream:
        stream.write("valid")
    with open(rule_paths.manifest, "w") as stream:
        stream.write("{")

    result = RuleGenerationManager(
        rule_paths, validator, "platform-v1", clock=lambda: 1234
    ).activate()

    assert result.source == "packaged"
    with open(rule_paths.manifest) as stream:
        manifest = json.load(stream)
    assert manifest["active_checksum"] == result.checksum


def test_invalid_inbox_does_not_displace_active_generation(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w") as stream:
        stream.write("active")
    with open(rule_paths.inbox, "w") as stream:
        stream.write("invalid")
    result = RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "active"
    assert result.fallback_used is False
    assert open(rule_paths.active).read() == "active"


def test_candidate_validator_exception_falls_back_to_active_generation(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.packaged, "w") as stream:
        stream.write("raises")
    with open(rule_paths.active, "w") as stream:
        stream.write("active")

    def raising_validator(path, dse):
        if open(path).read() == "raises":
            raise RuntimeError("vendor validator failed")
        return validator(path, dse)

    result = RuleGenerationManager(
        rule_paths, raising_validator, "new-platform"
    ).activate()

    assert result.source == "active"
    assert result.fallback_used is True
    assert open(rule_paths.active).read() == "active"


def test_fallback_preserves_rejected_inbox_activation_attempt(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w") as stream:
        stream.write("active")
    with open(rule_paths.inbox, "w") as stream:
        stream.write("zero")
    inbox_checksum = sha256_file(rule_paths.inbox)
    with open(rule_paths.watcher_state, "w") as stream:
        json.dump({"last_restart_checksum": inbox_checksum}, stream)

    def zero_rule_validator(path, dse):
        if open(path).read() == "zero":
            return CandidateValidation(
                True,
                0,
                "0.0.1",
                errors=("all signatures failed materialization",),
            )
        return validator(path, dse)

    result = RuleGenerationManager(
        rule_paths, zero_rule_validator, "platform-v1"
    ).activate()

    assert result.source == "active"
    assert result.fallback_used is True
    manifest = json.load(open(rule_paths.manifest))
    rejected, activated = manifest["activation_attempts"][-2:]
    assert rejected["source"] == "inbox"
    assert rejected["checksum"] == inbox_checksum
    assert rejected["validation_result"] == "FAILED"
    assert rejected["activation_result"] == "REJECTED"
    assert rejected["usable_rule_count"] == 0
    assert "zero usable rules" in rejected["reason"]
    assert "all signatures failed materialization" in rejected["reason"]
    assert activated["source"] == "active"
    assert activated["activation_result"] == "ACTIVATED"
    assert activated["fallback_used"] is True
    assert any(
        "inbox candidate rejected: zero usable rules" in reason
        for reason in activated["fallback_reasons"]
    )
    assert manifest["last_attempt"] == activated
    assert manifest["last_activation"]["fallback_reasons"] == (
        activated["fallback_reasons"]
    )


def test_activation_attempt_history_is_bounded_to_recent_records(tmp_path):
    manager = RuleGenerationManager(paths(tmp_path), validator, "platform-v1")
    manifest = {}

    for sequence in range(manager.MAX_ACTIVATION_ATTEMPTS + 5):
        manager._append_attempt(manifest, {"sequence": sequence})

    assert [item["sequence"] for item in manifest["activation_attempts"]] == list(
        range(5, manager.MAX_ACTIVATION_ATTEMPTS + 5)
    )
    assert manifest["last_attempt"] == {
        "sequence": manager.MAX_ACTIVATION_ATTEMPTS + 4
    }


def test_only_watcher_accepted_inbox_can_activate(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w") as stream:
        stream.write("active")
    with open(rule_paths.inbox, "w") as stream:
        stream.write("new")
    with open(rule_paths.watcher_state, "w") as stream:
        json.dump({"last_restart_checksum": sha256_file(rule_paths.inbox)}, stream)
    result = RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "inbox"
    assert open(rule_paths.active).read() == "new"


def test_inbox_is_promoted_from_the_immutable_validated_snapshot(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.inbox, "w") as stream:
        stream.write("validated")
    with open(rule_paths.watcher_state, "w") as stream:
        json.dump({"last_restart_checksum": sha256_file(rule_paths.inbox)}, stream)

    validated_paths = []

    def mutating_validator(path, _dse):
        validated_paths.append(path)
        content = open(path).read()
        with open(rule_paths.inbox, "w") as stream:
            stream.write("changed-after-staging")
        return CandidateValidation(True, 1, "0.0.1", payload=content)

    result = RuleGenerationManager(
        rule_paths, mutating_validator, "platform-v1"
    ).activate()

    assert validated_paths[0] != rule_paths.inbox
    assert open(rule_paths.active).read() == "validated"
    assert result.checksum == sha256_file(rule_paths.active)


def test_replaced_inbox_must_be_settled_by_watcher_before_activation(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.inbox, "w") as stream:
        stream.write("watcher-accepted")
    with open(rule_paths.active, "w") as stream:
        stream.write("active")
    with open(rule_paths.watcher_state, "w") as stream:
        json.dump({"last_restart_checksum": sha256_file(rule_paths.inbox)}, stream)

    class ReplacingManager(RuleGenerationManager):
        def _candidates(self, manifest):
            candidates = super()._candidates(manifest)
            with open(self.paths.inbox, "w") as stream:
                stream.write("not-yet-settled")
            return candidates

    result = ReplacingManager(
        rule_paths, validator, "platform-v1"
    ).activate()

    assert result.source == "active"
    assert result.fallback_used is True
    assert open(rule_paths.active).read() == "active"


def test_previous_active_generation_is_a_rollback_candidate(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(rule_paths.rules_dir)
    previous = os.path.join(rule_paths.rules_dir, "dld_rules.1-previous.yaml")
    with open(previous, "w") as stream:
        stream.write("previous")
    with open(rule_paths.active, "w") as stream:
        stream.write("invalid")
    with open(rule_paths.manifest, "w") as stream:
        json.dump(
            {
                "platform_identity": "platform-v1",
                "active_checksum": "sha256:bad",
                "previous_active_generation_path": previous,
            },
            stream,
        )
    result = RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "previous_active"
    assert result.fallback_used is True
    assert open(rule_paths.active).read() == "previous"


def test_zero_candidates_is_fatal(tmp_path):
    with pytest.raises(RuntimeError):
        RuleGenerationManager(paths(tmp_path), validator, "platform-v1").activate()


def test_watcher_requires_stability_and_restarts_once(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    with open(rule_paths.inbox, "w") as stream:
        stream.write("rules")
    restarts = []
    now = [100.0]
    watcher = RulesWatcher(
        rule_paths.inbox,
        rule_paths.lock,
        str(tmp_path / "watch.json"),
        settle_time=30,
        restart=lambda: restarts.append(True),
        clock=lambda: now[0],
    )
    assert watcher.check_once() is False
    now[0] += 31
    assert watcher.check_once() is True
    assert watcher.check_once() is False
    assert restarts == [True]
    state = json.load(open(tmp_path / "watch.json"))
    assert state["last_restart_requested_at"] == 131


def test_watcher_replaces_malformed_state_with_observation(tmp_path):
    rule_paths = paths(tmp_path)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    with open(rule_paths.inbox, "w") as stream:
        stream.write("rules")
    state_path = tmp_path / "watch.json"
    state_path.write_text("{")
    watcher = RulesWatcher(
        rule_paths.inbox,
        rule_paths.lock,
        str(state_path),
        clock=lambda: 100.0,
    )

    assert watcher.check_once() is False

    with open(state_path) as stream:
        state = json.load(stream)
    assert state["observation"]["size"] == 5
    assert state["first_seen"] == 100.0


def test_broken_rule_state_floors_external_timestamps(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    store = BrokenRuleStateStore(str(state_path))
    monkeypatch.setattr("dldd.lifecycle.time.time", lambda: 200.9)

    store.save(
        "sha256:test",
        ({"rule_id": 1, "last_attempt": 199.8},),
    )

    state = json.load(open(state_path))
    assert state["updated_at"] == 200
    assert state["broken_rules"][0]["last_attempt"] == 199


def test_watcher_queues_restart_without_waiting_on_its_partof_unit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "dldd.watcher.subprocess.run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    RulesWatcher._restart_service()

    assert calls == [
        (
            ["/bin/systemctl", "--no-block", "restart", "dldd.service"],
            {"shell": False, "check": True},
        )
    ]


def test_non_object_broken_rule_state_is_ignored_with_diagnostic(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("[]")

    state = BrokenRuleStateStore(str(state_path)).load(
        "sha256:test", allow_crash_recovery=True
    )

    assert state["broken_rules"] == []
    assert "root must be an object" in state["recovery_error"]


def test_malformed_broken_rule_records_are_ignored_with_diagnostic(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "state_schema": 1,
                "active_rules_checksum": "sha256:test",
                "clean_shutdown": False,
                "broken_rules": ["not-an-object"],
            }
        )
    )

    state = BrokenRuleStateStore(str(state_path)).load(
        "sha256:test", allow_crash_recovery=True
    )

    assert state["broken_rules"] == []
    assert "array of objects" in state["recovery_error"]
