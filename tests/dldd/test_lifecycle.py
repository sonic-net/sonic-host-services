from __future__ import absolute_import

import json
import os

import pytest

from dldd.lifecycle import (
    BrokenRuleStateStore,
    CandidateValidation,
    NoRulesAvailable,
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


def case_paths(tmp_path, name):
    """Create an isolated lifecycle layout for one scenario in a grouped test."""

    root = tmp_path / name
    root.mkdir()
    return paths(root)


def test_watcher_state_is_outside_remotely_writable_inbox(tmp_path):
    rule_paths = paths(tmp_path)

    assert os.path.dirname(rule_paths.watcher_state) == rule_paths.rules_dir
    assert os.path.dirname(rule_paths.watcher_state) != os.path.dirname(
        rule_paths.inbox
    )


def validator(path, _dse):
    content = open(path).read()
    valid = content != "invalid"
    return CandidateValidation(
        valid,
        1 if valid else 0,
        "0.0.1",
        errors=() if valid else ("invalid",),
        payload=content,
    )


def test_generation_promotion_fallback_and_history_contract(tmp_path):
    rule_paths = case_paths(tmp_path, "packaged-promotion")
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

    rule_paths = case_paths(tmp_path, "malformed-manifest")
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

    rule_paths = case_paths(tmp_path, "rollback")
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


    rule_paths = case_paths(tmp_path, "invalid-inbox")
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

    rule_paths = case_paths(tmp_path, "validator-exception")
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

    rule_paths = case_paths(tmp_path, "rejected-inbox-attempt")
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


def test_watcher_authorizes_an_immutable_settled_inbox(tmp_path):
    rule_paths = case_paths(tmp_path, "watcher-accepted")
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

    rule_paths = case_paths(tmp_path, "immutable-snapshot")
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

    rule_paths = case_paths(tmp_path, "replaced-inbox")
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


def test_candidate_presence_errors_are_distinct_from_no_rules(tmp_path):
    with pytest.raises(NoRulesAvailable, match="no rules candidates exist"):
        RuleGenerationManager(
            case_paths(tmp_path, "no-candidates"), validator, "platform-v1"
        ).activate()

    rule_paths = case_paths(tmp_path, "invalid-candidate")
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w", encoding="utf-8") as stream:
        stream.write("invalid")

    def reject(unused_path, unused_dse):
        return CandidateValidation(False, 0, errors=("invalid rules",))

    with pytest.raises(RuntimeError, match="invalid rules") as error:
        RuleGenerationManager(rule_paths, reject, "platform-v1").activate()

    assert not isinstance(error.value, NoRulesAvailable)

    for candidate_path in ("active", "inbox"):
        for candidate_kind in ("directory", "broken_symlink"):
            name = "{}-{}".format(candidate_path, candidate_kind)
            rule_paths = case_paths(tmp_path, name)
            os.makedirs(rule_paths.rules_dir)
            path = getattr(rule_paths, candidate_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if candidate_kind == "directory":
                os.makedirs(path)
            else:
                os.symlink(str(tmp_path / "missing-generation"), path)

            with pytest.raises(
                RuntimeError,
                match="{} candidate is not a regular file".format(
                    candidate_path
                ),
            ) as error:
                RuleGenerationManager(
                    rule_paths, validator, "platform-v1"
                ).activate()

            assert not isinstance(error.value, NoRulesAvailable), name


def test_watcher_stability_state_and_restart_contract(tmp_path, monkeypatch):
    rule_paths = case_paths(tmp_path, "stable-watcher")
    os.makedirs(os.path.dirname(rule_paths.inbox))
    with open(rule_paths.inbox, "w") as stream:
        stream.write("rules")
    restarts = []
    now = [100.0]
    watcher = RulesWatcher(
        rule_paths.inbox,
        rule_paths.lock,
        str(tmp_path / "stable-watch.json"),
        settle_time=30,
        restart=lambda: restarts.append(True),
        clock=lambda: now[0],
    )
    assert watcher.check_once() is False
    now[0] += 31
    assert watcher.check_once() is True
    assert watcher.check_once() is False
    assert restarts == [True]
    state = json.load(open(tmp_path / "stable-watch.json"))
    assert state["last_restart_requested_at"] == 131

    rule_paths = case_paths(tmp_path, "malformed-watcher-state")
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


def test_broken_rule_state_serialization_and_recovery_diagnostics(
    tmp_path, monkeypatch
):
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

    state_path.write_text("[]")

    state = BrokenRuleStateStore(str(state_path)).load(
        "sha256:test", allow_crash_recovery=True
    )

    assert state["broken_rules"] == []
    assert "root must be an object" in state["recovery_error"]

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
