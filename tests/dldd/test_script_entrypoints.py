"""Behavioral tests for the two installed DLDD executable wrappers."""

from __future__ import absolute_import

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dldd import cli as dldd_cli
from dldd import watcher as dldd_watcher


ROOT = Path(__file__).resolve().parents[2]
DAEMON = ROOT / "scripts" / "dldd"
WATCHER = ROOT / "scripts" / "dldd-rules-watch"


class _RecordingWatcher(object):
    instances = []
    result = False

    def __init__(self, inbox, lock, state, settle_time):
        self.arguments = (inbox, lock, state, settle_time)
        self.checked = False
        type(self).instances.append(self)

    def check_once(self):
        self.checked = True
        return type(self).result


def _load_watcher():
    namespace = runpy.run_path(str(WATCHER), run_name="dldd_rules_watch_test")
    namespace = namespace["main"].__globals__
    namespace["RulesWatcher"] = _RecordingWatcher
    _RecordingWatcher.instances = []
    return namespace


def test_installed_daemon_and_watcher_entrypoint_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(dldd_cli, "main", lambda: calls.append(True))

    namespace = runpy.run_path(str(DAEMON), run_name="dldd_entry_test")

    assert namespace["main"] is dldd_cli.main
    assert calls == []

    calls = []

    def main():
        calls.append(True)
        return 7

    monkeypatch.setattr(dldd_cli, "main", main)

    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(DAEMON), run_name="__main__")

    assert error.value.code == 7
    assert calls == [True]


    namespace = _load_watcher()
    namespace["detect_identity"] = lambda: pytest.fail(
        "identity lookup must not run with an explicit settle time"
    )
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

    namespace = _load_watcher()
    calls = []

    class Provider(object):
        def load(self):
            calls.append("config-db")
            return {"rules_inbox_settle_time": "23"}

    class Config(object):
        @classmethod
        def from_sources(cls, config_db, defaults):
            calls.append((config_db, defaults))
            return SimpleNamespace(rules_inbox_settle_time=23)

    namespace.update(
        detect_identity=lambda: SimpleNamespace(platform="test-platform"),
        load_vendor_defaults=lambda path: calls.append(path)
        or {"rules_inbox_settle_time": 19},
        ConfigDBProvider=Provider,
        DLDDConfig=Config,
    )
    monkeypatch.setattr(sys, "argv", [str(WATCHER)])

    assert namespace["main"]() == 0

    assert calls == [
        "/usr/share/sonic/device/test-platform/dldd-config.yaml",
        "config-db",
        (
            {"rules_inbox_settle_time": "23"},
            {"rules_inbox_settle_time": 19},
        ),
    ]
    assert _RecordingWatcher.instances[0].arguments[-1] == 23

    namespace = _load_watcher()

    class Config(object):
        def __init__(self):
            self.rules_inbox_settle_time = 31

    def unavailable_identity():
        raise RuntimeError("platform unavailable during early boot")

    namespace.update(detect_identity=unavailable_identity, DLDDConfig=Config)
    monkeypatch.setattr(sys, "argv", [str(WATCHER)])

    assert namespace["main"]() == 0
    assert _RecordingWatcher.instances[0].arguments[-1] == 31
    assert _RecordingWatcher.instances[0].checked is True

    _RecordingWatcher.instances = []
    _RecordingWatcher.result = True
    monkeypatch.setattr(dldd_watcher, "RulesWatcher", _RecordingWatcher)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(WATCHER), "--settle-time", "5"],
    )

    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(WATCHER), run_name="__main__")

    assert error.value.code == 0
    assert _RecordingWatcher.instances[0].arguments[-1] == 5
    assert _RecordingWatcher.instances[0].checked is True
    _RecordingWatcher.result = False
