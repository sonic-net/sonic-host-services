from __future__ import absolute_import

import signal
from types import SimpleNamespace

import pytest

from dldd import service as dldd_service
from dldd.dse import DSERegistry
from dldd.hooks import VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService, TelemetryUnavailable
from dldd.validation import ExactCompatibilityMatcher


def _service(tmp_path):
    return DLDDService(
        paths=RulePaths(str(tmp_path)),
        state_db=object(),
        extensions=PlatformExtensions(
            PlatformIdentity("test-platform", "product", "software"),
            DSERegistry(),
            VendorHookRegistry(),
            ExactCompatibilityMatcher(),
        ),
    )


def test_shutdown_releases_workers_persists_state_and_wires_signals(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    calls = []
    service.monitors = [
        SimpleNamespace(
            join=lambda timeout: calls.append(("monitor", timeout))
        )
    ]
    service.action_runner = SimpleNamespace(
        shutdown=lambda wait: calls.append(("action", wait))
    )
    service.async_collection_pool = SimpleNamespace(
        shutdown=lambda wait: calls.append(("async", wait))
    )
    service.artifact_client = SimpleNamespace(
        shutdown=lambda wait: calls.append(("artifact", wait))
    )
    service.activation = SimpleNamespace(checksum="sha256:test")
    service.orchestrator = SimpleNamespace(
        broken_rules={
            "broken": {"state": "BROKEN"},
            "degraded": {"state": "DEGRADED"},
        }
    )
    saved = []
    service.state_store = SimpleNamespace(
        save=lambda checksum, records, clean_shutdown: saved.append(
            (checksum, list(records), clean_shutdown)
        )
    )

    service.shutdown(clean_shutdown=True)

    assert service.stop_event.is_set()
    assert calls == [
        ("monitor", 5),
        ("action", False),
        ("async", False),
        ("artifact", False),
    ]
    assert saved == [("sha256:test", [{"state": "BROKEN"}], True)]

    handlers = {}
    observed = []

    class FakeService(object):
        def __init__(self, stop_event):
            self.stop_event = stop_event

        def run(self):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            observed.append(self.stop_event.is_set())

    monkeypatch.setattr(
        dldd_service.signal,
        "signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )
    monkeypatch.setattr(dldd_service, "DLDDService", FakeService)

    dldd_service.run_service()

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert observed == [True]


def test_startup_fault_reconciliation_retries_then_fails_at_the_limit(
    tmp_path, monkeypatch, caplog
):
    service = _service(tmp_path)
    calls = []

    def reconcile_once_then_recover():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary STATE_DB read failure")

    service.orchestrator = SimpleNamespace(
        reconcile_existing_faults=reconcile_once_then_recover
    )
    monkeypatch.setattr(dldd_service, "TELEMETRY_FAILURE_LIMIT", 3)
    monkeypatch.setattr(dldd_service, "TELEMETRY_RETRY_INTERVAL", 0)

    assert service._reconcile_existing_faults_at_startup() is True
    assert len(calls) == 2
    assert "startup FAULT_INFO reconciliation failed (1/3)" in caplog.text

    service = _service(tmp_path)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("persistent STATE_DB read failure")

    service.orchestrator = SimpleNamespace(reconcile_existing_faults=fail)
    monkeypatch.setattr(dldd_service, "TELEMETRY_FAILURE_LIMIT", 2)

    with pytest.raises(
        TelemetryUnavailable,
        match="STATE_DB fault reconciliation failed 2 consecutive times",
    ):
        service._reconcile_existing_faults_at_startup()

    assert len(calls) == 2
