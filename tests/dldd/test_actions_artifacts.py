from __future__ import absolute_import

import io
import json
import os
import subprocess
import tarfile
import threading
import time

import pytest

from dldd.actions import ActionExecutor, ActionRunner
from dldd.artifacts import FilesystemArtifactClient
from dldd.hooks import VendorHook, VendorHookRegistry


class BlockingExecutor(ActionExecutor):
    def __init__(self, release):
        self.release = release

    def execute(self, action, timeout):
        self.release.wait()
        return "late"


def test_timed_out_vendor_call_does_not_own_a_non_daemon_worker():
    release = threading.Event()
    runner = ActionRunner(BlockingExecutor(release), max_workers=1)
    try:
        future = runner.submit(
            "BLOCKING",
            ({"type": "vendor", "timeout": 0.01},),
            None,
        )
        result = future.result(timeout=1)
        assert result.state == "FAILED"
        assert "timed out" in result.last_error
        assert all(worker.daemon for worker in runner._workers)

        exhausted = runner.submit(
            "EXHAUSTED",
            ({"type": "vendor", "timeout": 0.01},),
            None,
        ).result(timeout=1)
        assert exhausted.state == "FAILED"
        assert "capacity is exhausted" in exhausted.last_error
    finally:
        runner.shutdown(wait=False)
        release.set()


def test_filesystem_artifact_worker_completes_opaque_archive(tmp_path):
    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        query_runner=lambda query: "diagnostic output",
        max_workers=1,
    )
    try:
        request = client.request(
            {"rule": "TEST", "component": "PSU0"},
            (),
            ({"type": "vendor"},),
        )
        deadline = time.time() + 2
        state = request
        while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
            time.sleep(0.01)
            state = client.status(request.artifact_id)

        assert state.state == "COMPLETED"
        assert os.path.isfile(tmp_path / request.artifact_id)
        assert all(worker.daemon for worker in client._workers)
    finally:
        client.shutdown(wait=True)


def test_artifact_logs_are_bounded_regular_files_without_recursion(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    regular = logs / "regular.log"
    regular.write_text("bounded log")
    nested = logs / "nested"
    nested.mkdir()
    (nested / "must-not-be-added.log").write_text("nested secret")
    link = logs / "link.log"
    link.symlink_to(regular)
    client = FilesystemArtifactClient(
        directory=str(tmp_path / "artifacts"),
        max_workers=1,
        max_artifact_bytes=4096,
    )
    try:
        request = client.request(
            {"rule": "TEST"},
            (str(regular), str(nested), str(link)),
            (),
        )
        deadline = time.time() + 2
        state = request
        while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
            time.sleep(0.01)
            state = client.status(request.artifact_id)

        assert state.state == "COMPLETED"
        archive_path = tmp_path / "artifacts" / request.artifact_id
        assert archive_path.stat().st_size <= 4096
        with tarfile.open(str(archive_path), "r:gz") as archive:
            names = archive.getnames()
        assert "logs/regular.log" in names
        assert "logs/link.log" not in names
        assert not any("must-not-be-added" in name for name in names)
    finally:
        client.shutdown(wait=True)


def test_declared_vendor_query_timeout_cannot_strand_artifact_worker(tmp_path):
    release = threading.Event()

    def blocking_query(query):
        release.wait()
        return "late"

    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        query_runner=blocking_query,
        max_workers=1,
    )
    try:
        request = client.request(
            {"rule": "TEST"},
            (),
            ({"type": "vendor", "timeout": 0.01},),
        )
        deadline = time.time() + 2
        state = request
        while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
            time.sleep(0.01)
            state = client.status(request.artifact_id)

        assert state.state == "FAILED"
        assert "timed out" in state.last_error
        assert all(worker.daemon for worker in client._workers)
    finally:
        client.shutdown(wait=False)
        release.set()


def test_artifact_capacity_never_prunes_active_jobs(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking_query(query):
        started.set()
        release.wait()
        return "complete"

    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        query_runner=blocking_query,
        max_workers=1,
        max_artifacts=2,
        max_artifact_bytes=4096,
    )
    try:
        first = client.request(
            {"rule": "FIRST"}, (), ({"type": "vendor"},)
        )
        assert started.wait(1)
        second = client.request(
            {"rule": "SECOND"}, (), ({"type": "vendor"},)
        )

        with pytest.raises(RuntimeError, match="capacity is exhausted"):
            client.request({"rule": "EXCESS"}, (), ())
        assert len(list(tmp_path.glob("dldd-*.json"))) == 2

        release.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            states = (
                client.status(first.artifact_id),
                client.status(second.artifact_id),
            )
            if all(state.state in ("COMPLETED", "FAILED") for state in states):
                break
            time.sleep(0.01)
        assert all(state.state == "COMPLETED" for state in states)

        replacement = client.request({"rule": "REPLACEMENT"}, (), ())
        deadline = time.time() + 2
        state = replacement
        while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
            time.sleep(0.01)
            state = client.status(replacement.artifact_id)
        assert state.state == "COMPLETED"
        assert len(list(tmp_path.glob("dldd-*.json"))) == 2
        assert len(list(tmp_path.glob("dldd-*.tar.gz"))) == 2
    finally:
        release.set()
        client.shutdown(wait=True)


def test_concurrent_artifact_requests_share_one_capacity_limit(tmp_path):
    release = threading.Event()
    start = threading.Barrier(9)
    accepted = []
    rejected = []
    results_lock = threading.Lock()

    def blocking_query(query):
        release.wait()
        return "complete"

    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        query_runner=blocking_query,
        max_workers=1,
        max_artifacts=3,
        max_artifact_bytes=4096,
    )

    def request_artifact(index):
        start.wait()
        try:
            request = client.request(
                {"rule": str(index)}, (), ({"type": "vendor"},)
            )
            with results_lock:
                accepted.append(request)
        except RuntimeError as error:
            with results_lock:
                rejected.append(str(error))

    threads = tuple(
        threading.Thread(target=request_artifact, args=(index,))
        for index in range(8)
    )
    try:
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=1)

        assert len(accepted) == 3
        assert len(rejected) == 5
        assert all("capacity is exhausted" in error for error in rejected)
        assert len(list(tmp_path.glob("dldd-*.json"))) == 3
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=1)
        client.shutdown(wait=True)

    assert len(list(tmp_path.glob("dldd-*.json"))) == 3
    assert len(list(tmp_path.glob("dldd-*.tar.gz"))) == 3


def _write_test_archive(path, metadata=None):
    data = json.dumps(metadata or {"rule": "TEST"}).encode("utf-8")
    with tarfile.open(str(path), "w:gz") as archive:
        info = tarfile.TarInfo("metadata.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def test_artifact_startup_reconciles_interrupted_and_orphaned_files(tmp_path):
    completed_base = "dldd-11111111111111111111111111111111"
    interrupted_base = "dldd-22222222222222222222222222222222"
    orphan_base = "dldd-33333333333333333333333333333333"
    invalid_base = "dldd-44444444444444444444444444444444"

    _write_test_archive(tmp_path / (completed_base + ".tar.gz"))
    (tmp_path / (completed_base + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": completed_base + ".tar.gz",
                "state": "RUNNING",
                "requested_at": 10.0,
                "completed_at": None,
                "last_error": "",
            }
        )
    )
    (tmp_path / (interrupted_base + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": interrupted_base + ".tar.gz",
                "state": "REQUESTED",
                "requested_at": 20.0,
                "completed_at": None,
                "last_error": "",
            }
        )
    )
    _write_test_archive(tmp_path / (orphan_base + ".tar.gz"))
    (tmp_path / (invalid_base + ".tar.gz")).write_bytes(b"not a tar archive")
    staged = tmp_path / ("." + interrupted_base + "-deadbeef.tar.gz")
    staged.write_bytes(b"partial")

    client = FilesystemArtifactClient(
        directory=str(tmp_path), max_workers=1, max_artifacts=4,
        max_artifact_bytes=4096,
    )
    try:
        recovered = client.status(completed_base + ".tar.gz")
        assert recovered.state == "COMPLETED"
        assert recovered.requested_at == 10.0

        interrupted = client.status(interrupted_base + ".tar.gz")
        assert interrupted.state == "FAILED"
        assert "interrupted" in interrupted.last_error

        orphan = client.status(orphan_base + ".tar.gz")
        assert orphan.state == "COMPLETED"
        assert not staged.exists()
        assert not (tmp_path / (invalid_base + ".tar.gz")).exists()
        assert not (tmp_path / (invalid_base + ".json")).exists()
        assert len(list(tmp_path.glob("dldd-*.json"))) == 3
    finally:
        client.shutdown(wait=True)


def test_direct_i2c_action_expands_list_valued_bus(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"0x01\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    output = ActionExecutor()._execute_i2c(
        {
            "path": {
                "i2c_type": "get",
                "bus": ["6", "7"],
                "chip_addr": "0x58",
                "command": "0x7A",
                "size": "b",
            }
        },
        5,
    )

    assert output == ["0x01", "0x01"]
    assert [call[3] for call in calls] == ["6", "7"]


class I2CBusHook(VendorHook):
    def __init__(self):
        self.validated = []

    def collect(self, operation):
        return None

    def execute_action(self, action):
        return {}

    def validate_source(self, operation):
        self.validated.append(operation["bus"])

    def resolve_i2c_bus(self, bus, operation):
        return {"IO-MUX-6": "6", "IO-MUX-7": "7"}[bus]


def test_direct_i2c_action_vendor_hook_resolves_logical_buses(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"0x01\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    hook = I2CBusHook()
    hooks = VendorHookRegistry()
    hooks.register("i2c", hook)
    output = ActionExecutor(hooks=hooks)._execute_i2c(
        {
            "path": {
                "i2c_type": "get",
                "bus": ["IO-MUX-6", "IO-MUX-7"],
                "chip_addr": "0x58",
                "command": "0x7A",
                "size": "b",
            }
        },
        5,
    )

    assert output == ["0x01", "0x01"]
    assert hook.validated == [["IO-MUX-6", "IO-MUX-7"]]
    assert [call[3] for call in calls] == ["6", "7"]
