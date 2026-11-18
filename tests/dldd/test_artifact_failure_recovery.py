import threading

import pytest

from dldd import artifacts
from dldd.artifacts import HostHealthzArtifactClient
from dldd.models import Operation


class RecordingHealthz:
    def __init__(self):
        self.reserved = []
        self.submitted = []
        self.failed = []

    def __call__(self, method, request):
        if method == "reserve_artifact":
            artifact_id = "artifact-{}".format(len(self.reserved))
            self.reserved.append(artifact_id)
            return {
                "artifact_id": artifact_id,
                "requested_at": 1234,
                "location": "/var/lib/sonic/healthz/" + artifact_id,
            }
        if method == "submit_artifact":
            self.submitted.append(request["artifact_id"])
            return {}
        if method == "fail_artifact":
            self.failed.append(request["artifact_id"])
            return {}
        raise AssertionError(method)


@pytest.mark.parametrize("configured_timeout, expected", [(None, 30.0), ("4", 4.0)])
def test_artifact_query_normalizes_timeout_for_cli_runner(
    configured_timeout, expected, monkeypatch
):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return "output"

    monkeypatch.setattr(artifacts, "run_checked_shell_free", run)
    query = {"type": "cli", "argv": ["diagnostic"]}
    if configured_timeout is not None:
        query["timeout"] = configured_timeout
    original = dict(query)
    client = HostHealthzArtifactClient(healthz_call=RecordingHealthz())
    try:
        assert client._run_bounded_query(query) == "output"
        assert query == original
        assert calls[0][1]["timeout"] == expected
    finally:
        client.shutdown()


def test_dse_default_timeout_holds_slot_until_late_call_exits(monkeypatch):
    monkeypatch.setattr(artifacts, "DEFAULT_QUERY_TIMEOUT", 0.01, raising=False)
    started = threading.Event()
    release = threading.Event()
    query_threads = []
    executed = []

    def blocked(operation):
        executed.append(operation.command)
        query_threads.append(threading.current_thread())
        started.set()
        release.wait(1)
        return "discarded late output"

    healthz = RecordingHealthz()
    client = HostHealthzArtifactClient(healthz_call=healthz, max_workers=1)
    try:
        query = Operation(type="dse", command="test:collect()", executor=blocked)
        reference = client.request({}, (), (query.as_runtime_payload(),))
        assert started.wait(1)
        client._jobs.join()
        assert healthz.failed == [reference.artifact_id]
        assert healthz.submitted == []

        blocked_reference = client.request({}, (), (query.as_runtime_payload(),))
        client._jobs.join()
        assert healthz.failed == [reference.artifact_id, blocked_reference.artifact_id]
        assert executed == ["test:collect()"]

        release.set()
        query_threads[0].join(timeout=1)
        assert not query_threads[0].is_alive()
        assert healthz.submitted == []

        recovered = Operation(type="dse", executor=lambda _operation: "ready")
        ready_reference = client.request({}, (), (recovered.as_runtime_payload(),))
        client._jobs.join()
        assert healthz.submitted == [ready_reference.artifact_id]
    finally:
        release.set()
        client.shutdown()


def test_artifact_queue_rejects_before_reserving_and_recovers():
    started = threading.Event()
    release = threading.Event()

    def blocked(_query):
        started.set()
        release.wait(1)
        return "output"

    healthz = RecordingHealthz()
    client = HostHealthzArtifactClient(
        query_runner=blocked, healthz_call=healthz, max_workers=1, max_jobs=1
    )
    try:
        first = client.request({}, (), ({"type": "vendor"},))
        assert started.wait(1)
        second = client.request({}, (), ())
        with pytest.raises(RuntimeError, match="queue is full"):
            client.request({}, (), ())
        assert healthz.reserved == [first.artifact_id, second.artifact_id]

        release.set()
        client._jobs.join()
        third = client.request({}, (), ())
        client._jobs.join()
        assert healthz.submitted == [first.artifact_id, second.artifact_id, third.artifact_id]
        assert healthz.failed == []
    finally:
        release.set()
        client.shutdown()


def test_artifact_backend_failures_leave_client_usable():
    healthz = RecordingHealthz()
    failures = {"reserve_artifact", "submit_artifact", "fail_artifact"}

    def call(method, request):
        if method in failures:
            failures.remove(method)
            raise RuntimeError("{} unavailable".format(method))
        return healthz(method, request)

    client = HostHealthzArtifactClient(healthz_call=call, max_workers=1)
    try:
        with pytest.raises(RuntimeError, match="reserve_artifact unavailable"):
            client.request({}, (), ())
        assert healthz.reserved == []
        assert client._jobs.empty()

        client.request({}, (), ())
        client._jobs.join()
        assert not failures
        assert healthz.submitted == []
        recovered = client.request({}, (), ())
        client._jobs.join()
        assert healthz.submitted == [recovered.artifact_id]
        assert healthz.failed == []
    finally:
        client.shutdown()


def test_invalid_reservation_response_releases_valid_artifact_id():
    healthz = RecordingHealthz()

    def call(method, request):
        response = healthz(method, request)
        if method == "reserve_artifact":
            response["requested_at"] = "invalid timestamp"
        return response

    client = HostHealthzArtifactClient(healthz_call=call)
    try:
        with pytest.raises(ValueError):
            client.request({}, (), ())
        assert healthz.failed == healthz.reserved
        assert healthz.submitted == []
        assert client._jobs.empty()
    finally:
        client.shutdown()
