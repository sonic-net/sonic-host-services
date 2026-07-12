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
    sha256_file,
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

    paths = _case_paths(tmp_path, "hash-failure")
    with open(paths.packaged, "w", encoding="utf-8") as stream:
        stream.write("rules")
    manager = RuleGenerationManager(paths, _valid, "platform")
    monkeypatch.setattr(
        dldd_lifecycle,
        "sha256_file",
        lambda unused_path: (_ for _ in ()).throw(OSError("hash read failed")),
    )
    monkeypatch.setattr(
        manager,
        "_archive_failed",
        lambda *unused_args: pytest.fail(
            "candidate without checksum was archived"
        ),
    )

    with pytest.raises(RuntimeError, match="hash read failed"):
        manager.activate()

    monkeypatch.setattr(dldd_lifecycle, "sha256_file", sha256_file)


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

    paths = _case_paths(tmp_path, "size-change")
    os.makedirs(paths.rules_dir)
    manager = RuleGenerationManager(paths, _valid, "platform")
    with monkeypatch.context() as patch:
        patch.setattr(dldd_lifecycle.os, "fstat", lambda unused_fd: first)
        patch.setattr(
            dldd_lifecycle.os.path,
            "getsize",
            lambda unused_path: first.st_size + 1,
        )
        with pytest.raises(RuntimeError, match="size changed"):
            manager._stage_candidate(str(source))

    paths = _case_paths(tmp_path, "missing-source")
    os.makedirs(paths.rules_dir)
    manager = RuleGenerationManager(paths, _valid, "platform")
    with pytest.raises(FileNotFoundError):
        manager._stage_candidate(str(tmp_path / "missing.yaml"))
    assert not list(
        (tmp_path / "missing-source" / "rules").glob(
            ".dld_rules.candidate.*"
        )
    )

    paths = _case_paths(tmp_path, "removed-temporary")
    os.makedirs(paths.rules_dir)
    manager = RuleGenerationManager(paths, _valid, "platform")

    def remove_then_fail(path):
        os.unlink(path)
        raise OSError("staged file disappeared")

    with monkeypatch.context() as patch:
        patch.setattr(dldd_lifecycle.os.path, "getsize", remove_then_fail)
        with pytest.raises(OSError, match="disappeared"):
            manager._stage_candidate(str(source))
    assert not list(
        (tmp_path / "removed-temporary" / "rules").glob(
            ".dld_rules.candidate.*"
        )
    )


def test_post_validation_failure_cleanup_attempt_and_archive_contract(
    tmp_path, monkeypatch, caplog
):
    paths = _case_paths(tmp_path, "removed-staged-snapshot")
    with open(paths.packaged, "w", encoding="utf-8") as stream:
        stream.write("rules")

    def remove_staged(path, unused_dse):
        os.unlink(path)
        return CandidateValidation(True, 1, "0.0.1")

    with pytest.raises(RuntimeError, match="No such file|no such file"):
        RuleGenerationManager(paths, remove_staged, "platform").activate()

    for preexisting_reason in (False, True):
        caplog.clear()
        name = "promotion-failure-{}".format(preexisting_reason)
        paths = _case_paths(tmp_path, name)
        with open(paths.packaged, "w", encoding="utf-8") as stream:
            stream.write("rules")
        manager = RuleGenerationManager(paths, _valid, "platform")
        message = "packaged candidate activation failed: promotion failed"
        original_record = manager._record_attempt

        with monkeypatch.context() as patch:
            if preexisting_reason:

                def record(*args, **kwargs):
                    attempt = original_record(*args, **kwargs)
                    attempt["errors"].append(message)
                    return attempt

                patch.setattr(manager, "_record_attempt", record)

            patch.setattr(
                manager,
                "_promote",
                lambda *unused_args: (_ for _ in ()).throw(
                    RuntimeError("promotion failed")
                ),
            )
            if not preexisting_reason:
                patch.setattr(
                    manager,
                    "_archive_failed",
                    lambda *unused_args: (_ for _ in ()).throw(
                        OSError("archive filesystem failed")
                    ),
                )

            with pytest.raises(RuntimeError, match="promotion failed"):
                manager.activate()

        manifest = json.loads(open(paths.manifest, encoding="utf-8").read())
        assert manifest["last_attempt"]["errors"].count(message) == 1, name
        assert manifest["last_attempt"]["activation_result"] == "FAILED", name
        if not preexisting_reason:
            assert "unable to archive failed packaged candidate" in caplog.text


def test_active_generation_reuse_and_candidate_deduplication(
    tmp_path, monkeypatch
):
    paths = _case_paths(tmp_path, "reuse-active")
    os.makedirs(paths.rules_dir)
    with open(paths.active, "w", encoding="utf-8") as stream:
        stream.write("active")
    checksum = sha256_file(paths.active)
    generation = os.path.join(paths.rules_dir, "existing-generation.yaml")
    with open(paths.manifest, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "platform_identity": "platform",
                "active_checksum": checksum,
                "active_generation_path": generation,
            },
            stream,
        )
    manager = RuleGenerationManager(paths, _valid, "platform")
    monkeypatch.setattr(
        manager,
        "_promote",
        lambda *unused_args: pytest.fail("unchanged active rules were promoted"),
    )

    result = manager.activate()

    assert result.source == "active"
    manifest = json.loads(open(paths.manifest, encoding="utf-8").read())
    assert manifest["active_generation_path"] == generation

    paths = _case_paths(tmp_path, "deduplicate-active")
    os.makedirs(paths.rules_dir)
    with open(paths.active, "w", encoding="utf-8") as stream:
        stream.write("active")
    manager = RuleGenerationManager(paths, _valid, "platform")

    candidates = manager._candidates(
        {
            "platform_identity": "platform",
            "previous_active_generation_path": paths.active,
        }
    )

    assert candidates.count(("active", paths.active)) == 1
    assert sum(path == paths.active for unused_source, path in candidates) == 1


def test_attempt_archive_and_generation_maintenance_contract(
    tmp_path, monkeypatch, caplog
):
    manager = RuleGenerationManager(
        _case_paths(tmp_path, "attempt-history"), _valid, "platform"
    )
    validation = CandidateValidation(
        True,
        0,
        errors=("zero usable rules",),
    )

    assert manager._record_attempt([], "packaged", "sha256:test", validation) == {}
    manager._append_attempt([], {"sequence": 1})

    manifest = {"activation_attempts": "invalid-history"}
    attempt = manager._record_attempt(
        manifest,
        "packaged",
        "sha256:test",
        validation,
    )

    assert attempt["errors"].count("zero usable rules") == 1
    assert manifest["activation_attempts"] == [attempt]

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
    manager._archive_failed(str(source), "sha256:abcdef", "packaged")

    assert len(
        list(
            (tmp_path / "failed-archive" / "rules").glob(
                "dld_rules.failed.*"
            )
        )
    ) == 1

    paths = _case_paths(tmp_path, "pruning")
    os.makedirs(paths.rules_dir)
    generation_paths = []
    for index in range(3):
        path = (
            tmp_path
            / "pruning"
            / "rules"
            / "dld_rules.{}.yaml".format(index)
        )
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (index, index))
        generation_paths.append(str(path))
    manager = RuleGenerationManager(paths, _valid, "platform", retention=2)
    attempted = []

    def fail_unlink(path):
        attempted.append(path)
        raise OSError("filesystem busy")

    monkeypatch.setattr(dldd_lifecycle.os, "unlink", fail_unlink)

    manager._prune_generations({})

    assert attempted == [generation_paths[0]]
    assert "unable to prune rules generation" in caplog.text


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
    assert store.load("sha256:test", allow_crash_recovery=True) == {
        "broken_rules": [],
        "service_broken_count": 0,
    }

    path.write_text("{", encoding="utf-8")
    assert "invalid state file" in store.load(
        "sha256:test", allow_crash_recovery=True
    )["recovery_error"]

    _write_state(path, clean_shutdown=True)
    assert store.load("sha256:test", True)["broken_rules"] == []

    _write_state(path, state_schema=2)
    assert "incompatible" in store.load("sha256:test", True)["recovery_error"]

    _write_state(path, active_rules_checksum="sha256:other")
    assert store.load("sha256:test", True)["broken_rules"] == []

    _write_state(path)
    recovered = store.load("sha256:test", True)
    assert recovered["broken_rules"] == [
        {"rule_id": 1000001, "state": "BROKEN"}
    ]

    path.write_text("{}", encoding="utf-8")

    store.clear()
    store.clear()

    assert not path.exists()
