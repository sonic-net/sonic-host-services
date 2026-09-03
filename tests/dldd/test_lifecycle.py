from __future__ import absolute_import

import json
import os

import pytest

from dldd.lifecycle import (
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


def write_accepted_inbox(rule_paths, content, active=None):
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    if active is not None:
        with open(rule_paths.active, "w") as stream:
            stream.write(active)
    with open(rule_paths.inbox, "w") as stream:
        stream.write(content)
    with open(rule_paths.watcher_state, "w") as stream:
        json.dump({"last_restart_checksum": sha256_file(rule_paths.inbox)}, stream)


def test_generation_promotion_without_automatic_rollback(tmp_path):
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
    assert os.path.isfile(manifest["active_generation_path"])

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
                "previous_active_checksum": "sha256:previous",
                "activation_attempts": [
                    {
                        "source": "active",
                        "previous_checksum": "sha256:previous",
                        "fallback_used": True,
                        "fallback_reasons": ["legacy"],
                        "rollback_used": True,
                    }
                ],
            },
            stream,
        )
    with pytest.raises(RuntimeError, match="no candidate produced"):
        RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert open(rule_paths.active).read() == "invalid"
    assert open(previous).read() == "previous"
    manifest = json.load(open(rule_paths.manifest))
    assert "previous_active_generation_path" not in manifest
    assert "previous_active_checksum" not in manifest
    assert set(manifest["activation_attempts"][0]) == {"source"}

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

    rule_paths = case_paths(tmp_path, "golden-fallback")
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w") as stream:
        stream.write("invalid")
    with open(rule_paths.golden, "w") as stream:
        stream.write("golden")
    with pytest.raises(RuntimeError, match="no candidate produced"):
        RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert open(rule_paths.active).read() == "invalid"

    rule_paths = case_paths(tmp_path, "invalid-inbox")
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.makedirs(rule_paths.rules_dir)
    with open(rule_paths.active, "w") as stream:
        stream.write("active")
    with open(rule_paths.inbox, "w") as stream:
        stream.write("invalid")
    result = RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "active"
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
    manifest = json.load(open(rule_paths.manifest))
    rejected, activated = manifest["activation_attempts"][-2:]
    assert rejected["source"] == "inbox"
    assert rejected["checksum"] == inbox_checksum
    assert rejected["validation_result"] == "FAILED"
    assert rejected["activation_result"] == "REJECTED"
    assert "zero usable rules" in rejected["reason"]
    assert activated["source"] == "active"
    assert activated["activation_result"] == "ACTIVATED"
    assert "fallback_used" not in activated
    assert "fallback_reasons" not in activated
    assert manifest["last_attempt"] == activated

    manager = RuleGenerationManager(
        case_paths(tmp_path, "bounded-history"), validator, "platform-v1"
    )
    history = {}
    for sequence in range(manager.MAX_ACTIVATION_ATTEMPTS + 1):
        manager._append_attempt(history, {"sequence": sequence})
    assert [item["sequence"] for item in history["activation_attempts"]] == list(
        range(1, manager.MAX_ACTIVATION_ATTEMPTS + 1)
    )


def test_watcher_authorizes_an_immutable_settled_inbox(tmp_path):
    rule_paths = case_paths(tmp_path, "watcher-accepted")
    write_accepted_inbox(rule_paths, "new", active="active")
    result = RuleGenerationManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "inbox"
    assert open(rule_paths.active).read() == "new"

    rule_paths = case_paths(tmp_path, "immutable-snapshot")
    write_accepted_inbox(rule_paths, "validated")

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
    write_accepted_inbox(rule_paths, "watcher-accepted", active="active")

    class ReplacingManager(RuleGenerationManager):
        def _candidates(self, manifest):
            candidates = super(ReplacingManager, self)._candidates(manifest)
            with open(self.paths.inbox, "w") as stream:
                stream.write("not-yet-settled")
            return candidates

    result = ReplacingManager(rule_paths, validator, "platform-v1").activate()
    assert result.source == "active"
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

    rule_paths = case_paths(tmp_path, "broken-inbox-symlink")
    os.makedirs(rule_paths.rules_dir)
    os.makedirs(os.path.dirname(rule_paths.inbox))
    os.symlink(str(tmp_path / "missing-generation"), rule_paths.inbox)
    with pytest.raises(
        RuntimeError, match="inbox candidate is not a regular file"
    ):
        RuleGenerationManager(rule_paths, validator, "platform-v1").activate()


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
