"""Behavioral tests for the two installed DLDD executable wrappers."""

from __future__ import absolute_import

import runpy
import sys
from pathlib import Path

import pytest

from dldd import cli as dldd_cli


ROOT = Path(__file__).resolve().parents[2]
DAEMON = ROOT / "scripts" / "dldd"
WATCHER = ROOT / "scripts" / "dldd-rules-watch"


class _RecordingWatcher(object):
    instances = []

    def __init__(self, inbox, lock, state, settle_time):
        self.arguments = (inbox, lock, state, settle_time)
        self.checked = False
        type(self).instances.append(self)

    def check_once(self):
        self.checked = True
        return False


def test_installed_daemon_and_watcher_entrypoint_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(dldd_cli, "main", lambda: calls.append(True))

    namespace = runpy.run_path(str(DAEMON), run_name="dldd_entry_test")

    assert namespace["main"] is dldd_cli.main
    assert calls == []

    monkeypatch.setattr(dldd_cli, "main", lambda: 7)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(DAEMON), run_name="__main__")
    assert error.value.code == 7

    namespace = runpy.run_path(
        str(WATCHER), run_name="dldd_rules_watch_test"
    )["main"].__globals__
    namespace["RulesWatcher"] = _RecordingWatcher
    namespace["detect_identity"] = lambda: pytest.fail(
        "explicit settle time must not require platform discovery"
    )
    _RecordingWatcher.instances = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(WATCHER),
            "--inbox",
            "/tmp/inbox.yaml",
            "--lock",
            "/tmp/watcher.lock",
            "--state",
            "/tmp/watcher.json",
            "--settle-time",
            "17",
        ],
    )

    assert namespace["main"]() == 0
    instance = _RecordingWatcher.instances[0]
    assert instance.arguments == (
        "/tmp/inbox.yaml",
        "/tmp/watcher.lock",
        "/tmp/watcher.json",
        17,
    )
    assert instance.checked is True
