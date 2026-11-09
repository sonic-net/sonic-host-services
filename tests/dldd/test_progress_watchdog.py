import os
import socket
import threading
from types import SimpleNamespace
from queue import Queue

import pytest

from dldd import service as service_module
from dldd.service import DLDDService


def test_watchdog_environment_selects_only_this_process(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/test/notify")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")
    assert service_module._watchdog_interval() == 30
    monkeypatch.setenv("WATCHDOG_USEC", "4000000")
    assert service_module._watchdog_interval() == 2
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert service_module._watchdog_interval() == 0
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    monkeypatch.setenv("WATCHDOG_USEC", "invalid")
    assert service_module._watchdog_interval() == 0
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert service_module._watchdog_interval() == 0


def test_watchdog_delivers_nonblocking_notification(monkeypatch):
    calls: list[object] = []

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *unused):
            calls.append("closed")

        def setblocking(self, enabled):
            assert enabled is False

        def sendto(self, payload, address):
            calls.append((payload, address))

    def channel(family, kind):
        assert (family, kind) == (socket.AF_UNIX, socket.SOCK_DGRAM)
        return Channel()

    monkeypatch.setattr(service_module.socket, "socket", channel)
    monkeypatch.setenv("NOTIFY_SOCKET", "@notify")
    service_module._notify_watchdog()
    assert calls == [(b"WATCHDOG=1", "\0notify"), "closed"]


def test_stalled_live_monitor_fails_without_creating_another_owner(monkeypatch):
    service = DLDDService.__new__(DLDDService)
    service.adapters = {}
    service.evidence_queue = Queue()
    service.stop_event = threading.Event()
    monkeypatch.setattr(service, "_publish_status", lambda: True)
    monitor = SimpleNamespace(
        is_alive=lambda: True, progress_at=100,
        plan=SimpleNamespace(monitor_id="common"),
    )
    monkeypatch.setattr(service, "monitors", [monitor], raising=False)
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 219)
    service._supervise_monitors()
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 221)
    with pytest.raises(RuntimeError, match="common made no progress"):
        service._supervise_monitors()
    assert service.monitors == [monitor]
    service.stop_event.set()
    service._supervise_monitors()


def test_primary_watchdog_runs_between_telemetry_heartbeats(monkeypatch):
    service = DLDDService.__new__(DLDDService)
    service.stop_event = threading.Event()
    service.orchestrator = None
    monkeypatch.setattr(service, "start", lambda: None)
    monkeypatch.setattr(service, "shutdown", lambda **unused: None)
    publications = []
    def publish_status():
        publications.append(True)
        return True

    monkeypatch.setattr(service, "_publish_status", publish_status)
    clock = iter([0, 0, 0, 1, 1, 2, 2])
    monkeypatch.setattr(service_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(service_module, "_watchdog_interval", lambda: 1)
    notifications = []

    def notify():
        notifications.append(True)
        if len(notifications) == 3:
            raise RuntimeError("notification unavailable")

    monkeypatch.setattr(service_module, "_notify_watchdog", notify)
    with pytest.raises(RuntimeError, match="notification unavailable"):
        service.run()
    assert len(notifications) == 3
    assert publications == [True]
