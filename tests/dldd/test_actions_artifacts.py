from __future__ import absolute_import

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from dldd.actions import ActionExecutor, ActionResult, ActionRunner
from dldd.artifacts import (
    ArtifactRequest,
    FilesystemArtifactClient,
    HealthzArtifactClient,
)
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import Operation


class BlockingExecutor(ActionExecutor):
    def __init__(self, release):
        self.release = release

    def execute(self, action, timeout):
        self.release.wait()
        return "late"


def wait_for_artifact(client, request, timeout=2):
    deadline = time.time() + timeout
    state = request
    while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
        time.sleep(0.01)
        state = client.status(request.artifact_id)
    return state


def test_action_payload_interface_and_executor_contract(monkeypatch):
    action = ActionResult("cli", "SUCCESS", 100.9, 101.8)
    artifact = ArtifactRequest("artifact", "COMPLETED", 102.7, 103.6)

    assert action.as_payload()["started_at"] == 100
    assert action.as_payload()["completed_at"] == 101
    assert artifact.as_payload()["requested_at"] == 102
    assert artifact.as_payload()["completed_at"] == 103

    client = HealthzArtifactClient()
    with pytest.raises(NotImplementedError):
        client.request({}, (), ())
    with pytest.raises(NotImplementedError):
        client.status("artifact")
    assert client.shutdown() is None

    payload = ActionResult(
        "cli",
        "FAILED",
        100.9,
        101.8,
        command=["check"],
        output="partial output",
        error="command failed",
    ).as_payload()

    assert payload == {
        "type": "cli",
        "status": "FAILED",
        "started_at": 100,
        "completed_at": 101,
        "command": ["check"],
        "output": "partial output",
        "error": "command failed",
    }


    # Materialized and CLI operation dispatch follows the same action contract.
    executor = ActionExecutor()
    with pytest.raises(ValueError, match="materialized operation"):
        executor.execute({"type": "dse", "executor": lambda value: value}, 1)

    operation = Operation(type="dse", command="sensor:reset()")
    assert executor.execute(
        {
            "type": "dse",
            "executor": lambda value: value,
            "materialized_operation": operation,
        },
        1,
    ) is operation

    executor = ActionExecutor()
    with pytest.raises(ValueError, match="requires argv"):
        executor.execute({"type": "cli", "argv": []}, 1)

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout_text=lambda *args: "complete",
            stderr_text=lambda *args: "",
        )

    monkeypatch.setattr("dldd.command_execution.run_shell_free", run)
    assert executor.execute(
        {"type": "cli", "argv": ["check"], "max_output_bytes": 17}, 2
    ) == "complete"
    assert calls == [
        (
            ["check"],
            {"timeout": 2, "max_output_bytes": 17, "runner": None},
        )
    ]

    monkeypatch.setattr(
        "dldd.command_execution.run_shell_free",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=3,
            stdout_text=lambda *args: "",
            stderr_text=lambda *args: "permission denied",
        ),
    )
    with pytest.raises(RuntimeError, match="exited 3: permission denied"):
        executor.execute({"type": "cli", "argv": ["check"]}, 2)


class RecordingActionHook(VendorHook):
    def __init__(self):
        self.actions = []

    def collect(self, operation):
        return None

    def execute_action(self, action):
        self.actions.append(action)
        return {"handled": action["type"]}


def test_action_dispatches_custom_i2c_and_named_vendor_hooks():
    i2c_calls = []
    assert ActionExecutor(i2c_action=i2c_calls.append).execute(
        {"type": "i2c", "path": {"i2c_type": "set"}}, 1
    ) is None
    assert i2c_calls == [{"type": "i2c", "path": {"i2c_type": "set"}}]

    hooks = VendorHookRegistry()
    vendor = RecordingActionHook()
    dse = RecordingActionHook()
    explicit = RecordingActionHook()
    hooks.register("vendor", vendor)
    hooks.register("dse", dse)
    hooks.register("explicit", explicit)
    executor = ActionExecutor(hooks=hooks)

    assert executor.execute({"type": "vendor"}, 1) == {"handled": "vendor"}
    assert executor.execute({"type": "dse"}, 1) == {"handled": "dse"}
    assert executor.execute(
        {"type": "vendor", "hook": "explicit"}, 1
    ) == {"handled": "vendor"}


def test_action_runner_timeout_and_capacity_contract():
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

    started = threading.Event()
    release = threading.Event()

    class BusyExecutor(ActionExecutor):
        def execute(self, action, timeout):
            started.set()
            release.wait()
            return "done"

    runner = ActionRunner(BusyExecutor(), max_workers=1)
    try:
        active = runner.submit(
            "ACTIVE", ({"type": "vendor", "timeout": 1},), None
        )
        assert started.wait(1)
        rejected = runner.submit(
            "REJECTED", ({"type": "vendor", "timeout": 1},), None
        ).result(timeout=1)
        assert rejected.state == "FAILED"
        assert rejected.actions == ()
        assert rejected.last_error == "action sequence capacity is exhausted"
        release.set()
        assert active.result(timeout=1).state == "COMPLETED"
    finally:
        release.set()
        runner.shutdown(wait=True)


class SequenceExecutor(ActionExecutor):
    def execute(self, action, timeout):
        if action.get("fail"):
            raise RuntimeError("vendor rejected action")
        return action.get("output", "ok")


def test_action_runner_sequence_cancel_worker_failure_and_shutdown_contract(
    monkeypatch,
):
    runner = ActionRunner(SequenceExecutor(), max_workers=1)
    try:
        missing = runner.submit(
            "MISSING_TIMEOUT", ({"type": "vendor"},), None
        ).result(timeout=1)
        assert missing.state == "FAILED"
        assert missing.last_error == "local action has no timeout"
        assert missing.actions[0].status == "FAILED"

        completed = runner.submit(
            "SUCCESS",
            ({"type": "vendor", "timeout": 1, "output": "repaired"},),
            None,
        ).result(timeout=1)
        assert completed.state == "COMPLETED"
        assert completed.actions[0].output == "repaired"

        failed = runner.submit(
            "FAILURE",
            ({"type": "vendor", "timeout": 1, "fail": True},),
            None,
        ).result(timeout=1)
        assert failed.state == "FAILED"
        assert failed.last_error == "vendor rejected action"
        assert failed.actions[0].error == "vendor rejected action"
    finally:
        runner.shutdown(wait=True)

    rejected = runner.submit(
        "AFTER_SHUTDOWN", ({"type": "vendor", "timeout": 1},), None
    )
    with pytest.raises(RuntimeError, match="shut down"):
        rejected.result(timeout=1)
    # Repeated shutdown is intentionally idempotent.
    runner.shutdown(wait=False)

    runner = ActionRunner(SequenceExecutor(), max_workers=1)
    cancelled = Future()
    assert cancelled.cancel()
    assert runner._sequence_slots.acquire(False)
    runner._jobs.put(
        (
            cancelled,
            "cancelled-worker",
            ({"type": "vendor", "timeout": 1},),
            None,
        )
    )
    runner._jobs.join()
    try:
        assert cancelled.cancelled()
        # The worker must release capacity even though it skipped execution.
        assert runner._sequence_slots.acquire(False)
        runner._sequence_slots.release()
    finally:
        runner.shutdown(wait=True)

    runner = ActionRunner(SequenceExecutor(), max_workers=1)

    def terminate(*args, **kwargs):
        raise KeyboardInterrupt("vendor aborted worker")

    monkeypatch.setattr(runner, "_run_sequence", terminate)
    try:
        result = runner.submit(
            "ABORT", ({"type": "vendor", "timeout": 1},), None
        )
        with pytest.raises(RuntimeError, match="action worker terminated"):
            result.result(timeout=1)
    finally:
        runner.shutdown(wait=True)


def test_filesystem_artifact_request_completion_failure_and_shutdown_contract(
    tmp_path, monkeypatch
):
    completed_dir = tmp_path / "completed"
    client = FilesystemArtifactClient(
        directory=str(completed_dir),
        query_runner=lambda query: "diagnostic output",
        max_workers=1,
    )
    try:
        request = client.request(
            {"rule": "TEST", "component": "PSU0", "timestamp": 123.9},
            (),
            ({"type": "vendor"},),
        )
        deadline = time.time() + 2
        state = request
        while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
            time.sleep(0.01)
            state = client.status(request.artifact_id)

        assert state.state == "COMPLETED"
        assert os.path.isfile(completed_dir / request.artifact_id)
        assert all(worker.daemon for worker in client._workers)
        with tarfile.open(
            str(completed_dir / request.artifact_id), "r:gz"
        ) as archive:
            metadata = json.load(archive.extractfile("metadata.json"))
        assert metadata["timestamp"] == 123
    finally:
        client.shutdown(wait=True)

    failed_dir = tmp_path / "failed"
    client = FilesystemArtifactClient(directory=str(failed_dir), max_workers=1)
    monkeypatch.setattr(
        "dldd.artifacts.atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("state disk is read-only")
        ),
    )
    try:
        with pytest.raises(OSError, match="read-only"):
            client.request({"rule": "TEST"}, (), ())
        assert client._active == set()
        assert list(failed_dir.iterdir()) == []
    finally:
        client.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shut down"):
        client.request({"rule": "TEST"}, (), ())
    client.shutdown(wait=True)


def test_artifact_metadata_query_and_archive_size_limit_contract(
    tmp_path, monkeypatch, caplog
):
    metadata_client = FilesystemArtifactClient(
        directory=str(tmp_path / "metadata"),
        max_workers=1,
        max_artifact_bytes=1024,
    )
    try:
        request = metadata_client.request(
            {"rule": "TEST", "blob": "x" * 2048}, (), ()
        )
        failed = wait_for_artifact(metadata_client, request)
        assert failed.state == "FAILED"
        assert "metadata exceeds" in failed.last_error
    finally:
        metadata_client.shutdown(wait=True)

    query_client = FilesystemArtifactClient(
        directory=str(tmp_path / "query"),
        query_runner=lambda query: os.urandom(2048),
        max_workers=1,
        max_artifact_bytes=1024,
    )
    try:
        request = query_client.request(
            {"rule": "TEST"}, (), ({"type": "vendor"},)
        )
        completed = wait_for_artifact(query_client, request)
        assert completed.state == "COMPLETED"
        with tarfile.open(
            str(tmp_path / "query" / request.artifact_id), "r:gz"
        ) as archive:
            assert not any(
                name.startswith("queries/") for name in archive.getnames()
            )
    finally:
        query_client.shutdown(wait=True)

    real_unlink = os.unlink

    def unlink_then_report_missing(path):
        real_unlink(path)
        raise FileNotFoundError(path)

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "archive-race"),
        max_workers=1,
        max_artifact_bytes=1024,
    )
    monkeypatch.setattr("dldd.artifacts.os.path.getsize", lambda path: 2048)
    monkeypatch.setattr("dldd.artifacts.os.unlink", unlink_then_report_missing)
    try:
        request = client.request({"rule": "TEST"}, (), ())
        failed = wait_for_artifact(client, request)
        assert failed.state == "FAILED"
        assert "generated artifact exceeds" in failed.last_error
    finally:
        client.shutdown(wait=True)


    # Worker, terminal-manifest, and temporary-file failures remain localized.
    client = FilesystemArtifactClient(
        directory=str(tmp_path / "collector-failure"), max_workers=1
    )
    monkeypatch.setattr(
        client,
        "_collect",
        lambda *args: (_ for _ in ()).throw(RuntimeError("collector crashed")),
    )
    try:
        request = client.request({"rule": "TEST"}, (), ())
        failed = wait_for_artifact(client, request)
        assert failed.state == "FAILED"
        assert failed.last_error == "collector crashed"
    finally:
        client.shutdown(wait=True)

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "terminal-failure"), max_workers=1
    )
    monkeypatch.setattr(
        client,
        "_collect",
        lambda *args: (_ for _ in ()).throw(RuntimeError("collector crashed")),
    )
    monkeypatch.setattr(
        client,
        "_record_terminal",
        lambda *args: (_ for _ in ()).throw(OSError("manifest write failed")),
    )
    try:
        request = client.request({"rule": "TEST"}, (), ())
        client._jobs.join()
        assert client.status(request.artifact_id).state == "REQUESTED"
        assert "unable to record failed artifact" in caplog.text
    finally:
        client.shutdown(wait=True)

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "tempfile-failure"), max_workers=1
    )
    real_mkstemp = tempfile.mkstemp

    def fail_archive_tempfile(*args, **kwargs):
        if kwargs.get("suffix") == ".tar.gz":
            raise OSError("no temporary space")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(
        "dldd.artifacts.tempfile.mkstemp",
        fail_archive_tempfile,
    )
    try:
        request = client.request({"rule": "TEST"}, (), ())
        failed = wait_for_artifact(client, request)
        assert failed.state == "FAILED"
        assert failed.last_error == "no temporary space"
    finally:
        client.shutdown(wait=True)


def test_artifact_log_collection_bounds_and_file_type_contract(tmp_path):
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

    empty = tmp_path / "empty.log"
    empty.write_bytes(b"")
    oversized = tmp_path / "oversized.log"
    oversized.write_bytes(b"1234")
    directory = tmp_path / "directory"
    directory.mkdir()
    archive_path = tmp_path / "logs.tar"

    with tarfile.open(str(archive_path), "w") as archive:
        assert FilesystemArtifactClient._add_log_file(
            archive, str(empty), 10
        ) == 0
        assert FilesystemArtifactClient._add_log_file(
            archive, str(oversized), 3
        ) == 0
        assert FilesystemArtifactClient._add_log_file(
            archive, str(directory), 10
        ) == 0
        assert FilesystemArtifactClient._add_log_file(
            archive, str(tmp_path / "missing.log"), 10
        ) == 0

    with tarfile.open(str(archive_path), "r") as archive:
        assert archive.getnames() == []


def test_artifact_query_validation_and_dispatch_contract(tmp_path, monkeypatch):
    client = FilesystemArtifactClient(directory=str(tmp_path), max_workers=1)
    try:
        with pytest.raises(ValueError, match="timeout must be positive"):
            client._run_bounded_query({"type": "vendor", "timeout": 0})
        with pytest.raises(ValueError, match="invalid DLDD artifact identifier"):
            client.status("../not-an-artifact.tar.gz")
    finally:
        client.shutdown(wait=True)

    with pytest.raises(ValueError, match="materialized operation"):
        FilesystemArtifactClient._run_query(
            {"type": "dse", "executor": lambda operation: operation}
        )

    operation = Operation(type="dse", command="sensor:collect()")
    assert FilesystemArtifactClient._run_query(
        {
            "type": "dse",
            "executor": lambda materialized: materialized,
            "materialized_operation": operation,
        }
    ) is operation
    with pytest.raises(RuntimeError, match="runner must be registered"):
        FilesystemArtifactClient._run_query({"type": "vendor"})

    calls = []

    def successful(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout_text=lambda *args: "diagnostics",
            stderr_text=lambda *args: "",
        )

    monkeypatch.setattr("dldd.command_execution.run_shell_free", successful)
    assert FilesystemArtifactClient._run_query(
        {
            "type": "cli",
            "argv": ["show", "platform"],
            "timeout": 3,
            "max_output_bytes": 19,
        }
    ) == "diagnostics"
    assert calls == [
        (
            ["show", "platform"],
            {"timeout": 3, "max_output_bytes": 19, "runner": None},
        )
    ]

    monkeypatch.setattr(
        "dldd.command_execution.run_shell_free",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout_text=lambda *args: "",
            stderr_text=lambda *args: "query failed",
        ),
    )
    with pytest.raises(RuntimeError, match="query failed"):
        FilesystemArtifactClient._run_query(
            {"type": "cli", "argv": ["show", "platform"]}
        )


    # A declared vendor timeout cannot strand the artifact worker.
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


def test_artifact_capacity_pruning_shutdown_and_concurrency_contract(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking_query(query):
        started.set()
        release.wait()
        return "complete"

    capacity_dir = tmp_path / "capacity"
    client = FilesystemArtifactClient(
        directory=str(capacity_dir),
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
        assert len(list(capacity_dir.glob("dldd-*.json"))) == 2

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
        assert len(list(capacity_dir.glob("dldd-*.json"))) == 2
        assert len(list(capacity_dir.glob("dldd-*.tar.gz"))) == 2
    finally:
        release.set()
        client.shutdown(wait=True)


    # Nonblocking shutdown tolerates a full worker queue.
    started = threading.Event()
    release = threading.Event()

    def blocking_query(query):
        started.set()
        release.wait()
        return "done"

    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        query_runner=blocking_query,
        max_workers=1,
        max_artifacts=1,
    )
    request = client.request(
        {"rule": "TEST"}, (), ({"type": "vendor"},)
    )
    assert started.wait(1)
    # A stop token already fills the one-slot queue. shutdown(wait=False)
    # must remain nonblocking when it cannot enqueue another token.
    client._jobs.put_nowait(None)
    client.shutdown(wait=False)
    release.set()
    for worker in client._workers:
        worker.join(timeout=1)

    assert not any(worker.is_alive() for worker in client._workers)
    assert client.status(request.artifact_id).state == "COMPLETED"


    # Concurrent requests share the same atomic capacity limit.
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


def test_artifact_startup_reconciliation_and_untrusted_file_contract(
    tmp_path, monkeypatch
):
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


    # Untrusted, malformed, oversized, and racy startup files fail closed.
    directory = tmp_path / "untrusted"
    directory.mkdir()
    bases = [
        "dldd-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "dldd-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "dldd-cccccccccccccccccccccccccccccccc",
        "dldd-dddddddddddddddddddddddddddddddd",
    ]
    target = directory / "target"
    target.write_text("not a manifest")
    unrelated = directory / "not-an-artifact.json"
    unrelated.write_text("operator data")
    (directory / (bases[0] + ".json")).symlink_to(target)
    (directory / (bases[1] + ".json")).write_text("[]")
    (directory / (bases[2] + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": "wrong.tar.gz",
                "state": "FAILED",
                "requested_at": 1,
            }
        )
    )
    (directory / (bases[3] + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": bases[3] + ".tar.gz",
                "state": "UNKNOWN",
                "requested_at": 1,
            }
        )
    )

    client = FilesystemArtifactClient(directory=str(directory), max_workers=1)
    try:
        assert target.exists()
        assert unrelated.read_text() == "operator data"
        assert all(
            not (directory / (base + ".json")).exists() for base in bases
        )
    finally:
        client.shutdown(wait=True)


    directory = tmp_path / "invalid-timestamps"
    directory.mkdir()
    completed = "dldd-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    interrupted = "dldd-ffffffffffffffffffffffffffffffff"
    _write_test_archive(directory / (completed + ".tar.gz"))
    (directory / (completed + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": completed + ".tar.gz",
                "state": "RUNNING",
                "requested_at": "not-a-time",
            }
        )
    )
    (directory / (interrupted + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": interrupted + ".tar.gz",
                "state": "REQUESTED",
                "requested_at": "not-a-time",
            }
        )
    )

    client = FilesystemArtifactClient(directory=str(directory), max_workers=1)
    try:
        completed_state = client.status(completed + ".tar.gz")
        interrupted_state = client.status(interrupted + ".tar.gz")
        assert completed_state.state == "COMPLETED"
        assert isinstance(completed_state.requested_at, float)
        assert interrupted_state.state == "FAILED"
        assert isinstance(interrupted_state.requested_at, float)
    finally:
        client.shutdown(wait=True)


    directory = tmp_path / "staged-race"
    directory.mkdir()
    staged_name = (
        ".dldd-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-race.tar.gz"
    )
    staged = directory / staged_name
    staged.write_bytes(b"partial")
    staged_directory = directory / (
        ".dldd-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-directory.tar.gz"
    )
    staged_directory.mkdir()
    real_lstat = os.lstat

    def raced_lstat(path):
        if os.path.basename(path) == staged_name:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr("dldd.artifacts.os.lstat", raced_lstat)
    client = FilesystemArtifactClient(directory=str(directory), max_workers=1)
    try:
        assert staged.exists()
        assert staged_directory.is_dir()
    finally:
        client.shutdown(wait=True)


    directory = tmp_path / "oversized"
    directory.mkdir()
    oversized = "dldd-11111111111111111111111111111111"
    failed = "dldd-22222222222222222222222222222222"
    (directory / (oversized + ".tar.gz")).write_bytes(b"x" * 2048)
    (directory / (failed + ".json")).write_text(
        json.dumps(
            {
                "artifact_id": failed + ".tar.gz",
                "state": "FAILED",
                "requested_at": 1,
                "completed_at": 2,
                "last_error": "expected failure",
            }
        )
    )

    client = FilesystemArtifactClient(
        directory=str(directory), max_workers=1, max_artifact_bytes=1024
    )
    try:
        assert not (directory / (oversized + ".tar.gz")).exists()
        retained = client.status(failed + ".tar.gz")
        assert retained.state == "FAILED"
        assert retained.last_error == "expected failure"
    finally:
        client.shutdown(wait=True)


def test_direct_i2c_action_expansion_validation_timeout_and_failure_contract(
    monkeypatch,
):
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

    executor = ActionExecutor()
    with pytest.raises(ValueError, match="requires get or set"):
        executor._execute_i2c(
            {"path": {"i2c_type": "probe", "bus": "6"}}, 1
        )

    clock = iter((10.0, 12.0))
    monkeypatch.setattr("dldd.actions.time.monotonic", lambda: next(clock))
    with pytest.raises(subprocess.TimeoutExpired):
        executor._execute_i2c(
            {
                "path": {
                    "i2c_type": "get",
                    "bus": "6",
                    "chip_addr": "0x58",
                    "command": "0x7A",
                }
            },
            1,
        )

    monkeypatch.setattr(
        "dldd.actions.run_shell_free",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout_text=lambda: "",
            stderr_text=lambda: "I/O error",
        ),
    )

    with pytest.raises(RuntimeError, match="I/O error"):
        ActionExecutor._execute_i2c_bus(
            {
                "chip_addr": "0x58",
                "command": "0x7A",
            },
            "get",
            "6",
            1,
        )


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
