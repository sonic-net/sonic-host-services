from __future__ import absolute_import

import io
import json
import os
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dldd.actions import ActionExecutor, ActionRunner
from dldd.artifacts import FilesystemArtifactClient
from dldd.command_execution import ShellFreeResult
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import Operation


def wait_for_artifact(client, request, timeout=2):
    deadline = time.time() + timeout
    state = request
    while state.state not in ("COMPLETED", "FAILED") and time.time() < deadline:
        time.sleep(0.01)
        state = client.status(request.artifact_id)
    return state


class RecordingActionHook(VendorHook):
    def __init__(self):
        self.actions = []

    def collect(self, operation):
        return None

    def execute_action(self, action):
        self.actions.append(action)
        return {"handled": action["type"]}


def test_action_executor_cli_i2c_and_vendor_hook_contract(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return ShellFreeResult(tuple(argv), 0, b"complete", b"")

    monkeypatch.setattr("dldd.command_execution.run_shell_free", run)
    assert ActionExecutor().execute(
        {"type": "cli", "argv": ["check"], "max_output_bytes": 17}, 2
    ) == "complete"
    expected_call = (
        ["check"],
        {"timeout": 2, "max_output_bytes": 17, "runner": None},
    )
    assert calls == [expected_call]
    assert FilesystemArtifactClient._run_query(
        {
            "type": "cli",
            "argv": ["check"],
            "timeout": 2,
            "max_output_bytes": 17,
        }
    ) == "complete"
    assert calls == [expected_call, expected_call]

    operation = Operation(type="dse", command="sensor:collect()")
    resolved = {
        "type": "dse",
        "executor": lambda materialized: materialized,
        "materialized_operation": operation,
    }
    assert ActionExecutor().execute(resolved, 1) is operation
    assert FilesystemArtifactClient._run_query(resolved) is operation

    i2c_calls = []
    assert ActionExecutor(i2c_action=i2c_calls.append).execute(
        {"type": "i2c", "path": {"i2c_type": "set"}}, 1
    ) is None
    assert len(i2c_calls) == 1

    hooks = VendorHookRegistry()
    explicit = RecordingActionHook()
    hooks.register("explicit", explicit)
    result = ActionExecutor(hooks=hooks).execute(
        {"type": "vendor", "hook": "explicit"}, 1
    )
    assert result == {"handled": "vendor"}
    assert explicit.actions[0]["hook"] == "explicit"


class SequenceExecutor(ActionExecutor):
    def __init__(self):
        self.executed = []

    def execute(self, action, timeout):
        self.executed.append((action["name"], timeout))
        if action.get("fail"):
            raise RuntimeError("vendor rejected action")
        return action["name"] + "-complete"


class BlockingExecutor(ActionExecutor):
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def execute(self, action, timeout):
        self.started.set()
        self.release.wait()
        return "late"


def test_action_runner_order_timeout_and_capacity_contract():
    executor = SequenceExecutor()
    runner = ActionRunner(executor, max_workers=1)
    try:
        result = runner.submit(
            "ORDERED",
            (
                {"type": "vendor", "name": "first"},
                {
                    "type": "vendor",
                    "name": "second",
                    "timeout": 2,
                    "fail": True,
                },
                {"type": "vendor", "name": "must-not-run"},
            ),
            1,
        ).result(timeout=1)
        assert executor.executed == [("first", 1.0), ("second", 2.0)]
        assert result.state == "FAILED"
        assert [action.status for action in result.actions] == [
            "SUCCESS",
            "FAILED",
        ]
        assert result.actions[0].output == "first-complete"
        assert result.actions[1].error == "vendor rejected action"
        assert result.last_error == "vendor rejected action"
    finally:
        runner.shutdown(wait=True)

    started = threading.Event()
    release = threading.Event()
    runner = ActionRunner(BlockingExecutor(started, release), max_workers=1)
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
        assert "sequence capacity is exhausted" in rejected.last_error
        release.set()
        assert active.result(timeout=1).state == "COMPLETED"
    finally:
        release.set()
        runner.shutdown(wait=True)

    started = threading.Event()
    release = threading.Event()
    runner = ActionRunner(BlockingExecutor(started, release), max_workers=1)
    try:
        timed_out = runner.submit(
            "TIMEOUT", ({"type": "vendor", "timeout": 0.01},), None
        ).result(timeout=1)
        assert timed_out.state == "FAILED"
        assert "timed out" in timed_out.last_error

        exhausted = runner.submit(
            "EXHAUSTED", ({"type": "vendor", "timeout": 0.01},), None
        ).result(timeout=1)
        assert exhausted.state == "FAILED"
        assert exhausted.last_error == (
            "action execution capacity is exhausted by timed-out vendor calls"
        )
        assert [action.status for action in exhausted.actions] == ["FAILED"]

        runner.shutdown(wait=False)
        closed = runner.submit(
            "CLOSED", ({"type": "vendor", "timeout": 1},), None
        )
        with pytest.raises(RuntimeError, match="shut down"):
            closed.result(timeout=1)
    finally:
        release.set()
        runner.shutdown(wait=True)


def test_filesystem_artifact_request_completion_failure_and_shutdown_contract(
    tmp_path, monkeypatch
):
    logs = tmp_path / "logs"
    logs.mkdir()
    regular = logs / "regular.log"
    regular.write_text("bounded log", encoding="utf-8")
    oversized = logs / "oversized.log"
    oversized.write_bytes(b"x" * 2048)
    link = logs / "link.log"
    link.symlink_to(regular)
    nested = logs / "nested"
    nested.mkdir()
    (nested / "must-not-be-added.log").write_text("secret", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()

    def query_runner(query):
        started.set()
        assert release.wait(2), "artifact collection blocked its request"
        return "diagnostic output"

    directory = tmp_path / "completed"
    client = FilesystemArtifactClient(
        directory=str(directory),
        query_runner=query_runner,
        max_workers=1,
        max_artifact_bytes=1024,
    )
    try:
        request = client.request(
            {"rule": "TEST", "component": "PSU0"},
            (str(regular), str(oversized), str(link), str(nested)),
            ({"type": "vendor"},),
        )
        assert request.state == "REQUESTED"
        assert started.wait(1)
        release.set()

        completed = wait_for_artifact(client, request)
        assert completed.state == "COMPLETED"
        archive_path = directory / request.artifact_id
        assert archive_path.stat().st_size <= 1024
        with tarfile.open(str(archive_path), "r:gz") as archive:
            assert json.load(archive.extractfile("metadata.json")) == {
                "component": "PSU0",
                "rule": "TEST",
            }
            assert archive.extractfile("queries/000.txt").read() == (
                b"diagnostic output"
            )
            names = archive.getnames()
        assert "logs/regular.log" in names
        assert "logs/oversized.log" not in names
        assert "logs/link.log" not in names
        assert not any("must-not-be-added" in name for name in names)
    finally:
        release.set()
        client.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shut down"):
        client.request({"rule": "TEST"}, (), ())

    failed_directory = tmp_path / "initial-failure"
    client = FilesystemArtifactClient(
        directory=str(failed_directory), max_workers=1
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                "dldd.artifacts.atomic_write_json",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    OSError("state disk is read-only")
                ),
            )
            with pytest.raises(OSError, match="read-only"):
                client.request({"rule": "TEST"}, (), ())
            assert list(failed_directory.iterdir()) == []

        request = client.request({"rule": "RECOVERED"}, (), ())
        assert wait_for_artifact(client, request).state == "COMPLETED"
    finally:
        client.shutdown(wait=True)


def test_artifact_query_timeout_size_and_identifier_contract(
    tmp_path, monkeypatch
):
    release = threading.Event()

    def blocking_query(query):
        assert release.wait(2), "timed-out query was not released"
        return "late"

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "timeout"),
        query_runner=blocking_query,
        max_workers=1,
    )
    try:
        request = client.request(
            {"rule": "TEST"},
            (),
            ({"type": "vendor", "timeout": 0.01},),
        )
        failed = wait_for_artifact(client, request)
        assert failed.state == "FAILED"
        assert "timed out" in failed.last_error

        request = client.request(
            {"rule": "EXHAUSTED"},
            (),
            ({"type": "vendor", "timeout": 0.01},),
        )
        exhausted = wait_for_artifact(client, request)
        assert exhausted.last_error == (
            "artifact query capacity is exhausted by timed-out vendor calls"
        )
        with pytest.raises(ValueError, match="invalid DLDD artifact identifier"):
            client.status("../dldd-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tar.gz")
    finally:
        release.set()
        client.shutdown(wait=True)

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "size"),
        max_workers=1,
        max_artifact_bytes=1024,
    )
    try:
        request = client.request(
            {"rule": "TEST", "blob": "x" * 2048}, (), ()
        )
        failed = wait_for_artifact(client, request)
        assert failed.state == "FAILED"
        assert "metadata exceeds" in failed.last_error

        client.query_runner = lambda query: os.urandom(2048)
        request = client.request(
            {"rule": "TEST"}, (), ({"type": "vendor"},)
        )
        completed = wait_for_artifact(client, request)
        assert completed.state == "COMPLETED"
        with tarfile.open(
            str(tmp_path / "size" / request.artifact_id), "r:gz"
        ) as archive:
            assert not any(
                name.startswith("queries/") for name in archive.getnames()
            )
    finally:
        client.shutdown(wait=True)

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "physical-size"),
        max_workers=1,
        max_artifact_bytes=1024,
    )
    with monkeypatch.context() as patch:
        patch.setattr("dldd.artifacts.os.path.getsize", lambda path: 2048)
        try:
            request = client.request({"rule": "TEST"}, (), ())
            failed = wait_for_artifact(client, request)
            assert failed.state == "FAILED"
            assert "generated artifact exceeds" in failed.last_error
        finally:
            client.shutdown(wait=True)


def test_artifact_capacity_pruning_shutdown_and_concurrency_contract(tmp_path):
    release = threading.Event()

    def blocking_query(query):
        assert release.wait(2), "capacity test did not release query"
        return "complete"

    directory = tmp_path / "capacity"
    client = FilesystemArtifactClient(
        directory=str(directory),
        query_runner=blocking_query,
        max_workers=1,
        max_artifacts=2,
        max_artifact_bytes=4096,
    )
    accepted = []
    rejected = []
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(
                    client.request,
                    {"rule": str(index)},
                    (),
                    ({"type": "vendor"},),
                )
                for index in range(5)
            ]
            for future in futures:
                try:
                    accepted.append(future.result(timeout=1))
                except RuntimeError as error:
                    rejected.append(str(error))

        assert len(accepted) == 2
        assert len(rejected) == 3
        assert all("capacity is exhausted" in error for error in rejected)
        assert len(list(directory.glob("dldd-*.json"))) == 2

        release.set()
        assert all(
            wait_for_artifact(client, request).state == "COMPLETED"
            for request in accepted
        )

        replacement = client.request({"rule": "REPLACEMENT"}, (), ())
        assert wait_for_artifact(client, replacement).state == "COMPLETED"
        assert len(list(directory.glob("dldd-*.json"))) == 2
        assert len(list(directory.glob("dldd-*.tar.gz"))) == 2
    finally:
        release.set()
        client.shutdown(wait=True)


def _write_test_archive(path, metadata=None):
    data = json.dumps(metadata or {"rule": "TEST"}).encode("utf-8")
    with tarfile.open(str(path), "w:gz") as archive:
        info = tarfile.TarInfo("metadata.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def _write_manifest(path, artifact_base, state, requested_at=10.0):
    path.write_text(
        json.dumps(
            {
                "artifact_id": artifact_base + ".tar.gz",
                "state": state,
                "requested_at": requested_at,
                "completed_at": None,
                "last_error": "",
            }
        ),
        encoding="utf-8",
    )


def test_artifact_startup_reconciliation_and_untrusted_file_contract(tmp_path):
    completed = "dldd-11111111111111111111111111111111"
    interrupted = "dldd-22222222222222222222222222222222"
    corrupt = "dldd-33333333333333333333333333333333"
    untrusted = "dldd-44444444444444444444444444444444"
    linked_archive = "dldd-55555555555555555555555555555555"

    _write_test_archive(tmp_path / (completed + ".tar.gz"))
    _write_manifest(tmp_path / (completed + ".json"), completed, "RUNNING")
    _write_manifest(
        tmp_path / (interrupted + ".json"), interrupted, "REQUESTED", 20.0
    )
    (tmp_path / (corrupt + ".tar.gz")).write_bytes(b"not a tar archive")

    target = tmp_path / "operator-state"
    _write_manifest(target, untrusted, "FAILED")
    (tmp_path / (untrusted + ".json")).symlink_to(target)
    archive_target = tmp_path / "operator-archive.tar.gz"
    _write_test_archive(archive_target)
    (tmp_path / (linked_archive + ".tar.gz")).symlink_to(archive_target)
    staged = tmp_path / ("." + interrupted + "-partial.tar.gz")
    staged.write_bytes(b"partial")

    client = FilesystemArtifactClient(
        directory=str(tmp_path),
        max_workers=1,
        max_artifacts=4,
        max_artifact_bytes=4096,
    )
    try:
        assert client.status(completed + ".tar.gz").state == "COMPLETED"
        failed = client.status(interrupted + ".tar.gz")
        assert failed.state == "FAILED"
        assert "interrupted" in failed.last_error
        assert not (tmp_path / (corrupt + ".tar.gz")).exists()
        assert not (tmp_path / (untrusted + ".json")).exists()
        assert json.loads(target.read_text(encoding="utf-8"))["state"] == "FAILED"
        assert not (tmp_path / (linked_archive + ".tar.gz")).exists()
        assert archive_target.exists()
        assert not staged.exists()
    finally:
        client.shutdown(wait=True)


def test_direct_i2c_action_expansion_validation_timeout_and_failure_contract(
    monkeypatch,
):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"0x01\n", stderr=b"")

    clock = iter((10.0, 11.0, 13.0))
    monkeypatch.setattr("dldd.actions.time.monotonic", lambda: next(clock))
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
    assert [call[0][3] for call in calls] == ["6", "7"]
    assert [call[1]["timeout"] for call in calls] == [4.0, 2.0]


class I2CBusHook(VendorHook):
    def __init__(self):
        self.validated = []
        self.resolved = []

    def collect(self, operation):
        return None

    def execute_action(self, action):
        return {}

    def validate_source(self, operation):
        self.validated.append(operation["bus"])

    def resolve_i2c_bus(self, bus, operation):
        self.resolved.append(bus)
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
    assert hook.resolved == ["IO-MUX-6", "IO-MUX-7"] * 2
    assert [call[3] for call in calls] == ["6", "7"]
