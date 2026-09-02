from __future__ import absolute_import

import json
import os
from types import SimpleNamespace

import pytest

from dldd import lifecycle as dldd_lifecycle
from dldd.lifecycle import (
    BrokenRuleStateStore,
    CandidateValidation,
    RuleGenerationManager,
    RulePaths,
)


def _paths(tmp_path):
    platform = tmp_path / "platform"
    platform.mkdir()
    return RulePaths(
        str(platform),
        inbox=str(tmp_path / "inbox" / "dld_rules.yaml"),
        rules_dir=str(tmp_path / "rules"),
        state_file=str(tmp_path / "state.json"),
    )


def _case_paths(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    return _paths(root)


def _valid(path, unused_dse):
    return CandidateValidation(
        True,
        1,
        "0.0.1",
        payload=open(path, encoding="utf-8").read(),
    )


def test_activation_staging_validation_and_cleanup_contract(
    tmp_path, monkeypatch
):
    paths = _case_paths(tmp_path, "staging-failure")
    with open(paths.packaged, "w", encoding="utf-8") as stream:
        stream.write("rules")
    manager = RuleGenerationManager(paths, _valid, "platform")
    monkeypatch.setattr(
        manager,
        "_stage_candidate",
        lambda unused_path: (_ for _ in ()).throw(
            OSError("promotion filesystem is read-only")
        ),
    )

    with pytest.raises(RuntimeError, match="could not be staged"):
        manager.activate()

    manifest = json.loads(open(paths.manifest, encoding="utf-8").read())
    assert manifest["last_attempt"]["activation_result"] == "FAILED"
    assert "read-only" in manifest["last_attempt"]["reason"]

    paths = _case_paths(tmp_path, "validation-mutation")
    with open(paths.packaged, "w", encoding="utf-8") as stream:
        stream.write("rules")

    def mutate(path, unused_dse):
        os.chmod(path, 0o640)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("-changed")
        return CandidateValidation(True, 1, "0.0.1")

    with pytest.raises(RuntimeError, match="changed while it was being validated"):
        RuleGenerationManager(paths, mutate, "platform").activate()

    paths = _case_paths(tmp_path, "identity-change")
    os.makedirs(paths.rules_dir)
    source = tmp_path / "identity-source.yaml"
    source.write_text("rules", encoding="utf-8")
    manager = RuleGenerationManager(paths, _valid, "platform")
    first = SimpleNamespace(st_dev=1, st_ino=2, st_size=5, st_mtime_ns=3)
    changed = SimpleNamespace(st_dev=1, st_ino=4, st_size=5, st_mtime_ns=3)
    with monkeypatch.context() as patch:
        stats = iter((first, changed))
        patch.setattr(
            dldd_lifecycle.os, "fstat", lambda unused_fd: next(stats)
        )
        with pytest.raises(
            RuntimeError, match="changed while it was being staged"
        ):
            manager._stage_candidate(str(source))
    assert not list(
        (tmp_path / "identity-change" / "rules").glob(
            ".dld_rules.candidate.*"
        )
    )


def test_post_validation_failure_cleanup_attempt_and_archive_contract(
    tmp_path, monkeypatch, caplog
):
    paths = _case_paths(tmp_path, "promotion-failure")
    with open(paths.packaged, "w", encoding="utf-8") as stream:
        stream.write("rules")
    manager = RuleGenerationManager(paths, _valid, "platform")
    monkeypatch.setattr(
        manager,
        "_promote",
        lambda *unused_args: (_ for _ in ()).throw(
            RuntimeError("promotion failed")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_archive_failed",
        lambda *unused_args: (_ for _ in ()).throw(
            OSError("archive filesystem failed")
        ),
    )

    with pytest.raises(RuntimeError, match="promotion failed"):
        manager.activate()

    manifest = json.loads(open(paths.manifest, encoding="utf-8").read())
    attempt = manifest["last_attempt"]
    assert attempt["activation_result"] == "FAILED"
    assert "promotion failed" in attempt["reason"]
    assert "unable to archive failed packaged candidate" in caplog.text


def test_failed_archive_and_generation_retention_contract(tmp_path):
    paths = _case_paths(tmp_path, "failed-archive")
    os.makedirs(paths.rules_dir)
    source = tmp_path / "failed-archive-candidate.yaml"
    source.write_text("invalid", encoding="utf-8")
    manager = RuleGenerationManager(
        paths,
        _valid,
        "platform",
        clock=lambda: 100,
    )

    manager._archive_failed(str(source), "sha256:abcdef", "packaged")
    assert len(list((tmp_path / "failed-archive" / "rules").glob(
        "dld_rules.failed.*"
    ))) == 1

    paths = _case_paths(tmp_path, "pruning")
    os.makedirs(paths.rules_dir)
    generation_paths = []
    for index in range(9):
        path = (
            tmp_path
            / "pruning"
            / "rules"
            / "dld_rules.{}.yaml".format(index)
        )
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (index, index))
        generation_paths.append(str(path))
    manager = RuleGenerationManager(paths, _valid, "platform")
    manager._prune_generations(
        {
            "active_generation_path": generation_paths[0],
            "previous_active_generation_path": generation_paths[1],
        }
    )

    retained = {path for path in generation_paths if os.path.exists(path)}
    assert retained == set(generation_paths[:2] + generation_paths[4:])


def _write_state(path, **overrides):
    state = {
        "state_schema": 1,
        "active_rules_checksum": "sha256:test",
        "broken_rules": [{"rule_id": 1000001, "state": "BROKEN"}],
        "service_broken_count": 1,
        "clean_shutdown": False,
    }
    state.update(overrides)
    path.write_text(json.dumps(state), encoding="utf-8")


def test_broken_rule_state_recovery_and_clear_contract(tmp_path):
    path = tmp_path / "state.json"
    store = BrokenRuleStateStore(str(path))

    assert store.load("sha256:test", allow_crash_recovery=False) == {
        "broken_rules": [],
        "service_broken_count": 0,
    }

    _write_state(path, clean_shutdown=True)
    assert store.load("sha256:test", True)["broken_rules"] == []

    _write_state(path, state_schema=2)
    assert "incompatible" in store.load("sha256:test", True)["recovery_error"]

    _write_state(path, active_rules_checksum="sha256:other")
    assert store.load("sha256:test", True)["broken_rules"] == []

    store.save(
        "sha256:test",
        ({"rule_id": 1000001, "state": "BROKEN"},),
    )
    recovered = store.load("sha256:test", True)
    assert recovered["broken_rules"] == [
        {"rule_id": 1000001, "state": "BROKEN"}
    ]

    path.write_text("{}", encoding="utf-8")

    store.clear()
    assert not path.exists()
