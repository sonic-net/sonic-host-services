from pathlib import Path
import json
import subprocess
import tarfile
import threading
import time

from dldd.actions import ActionExecutor, ActionRunner
from dldd.artifacts import FilesystemArtifactClient
from dldd.hooks import VendorHook, VendorHookRegistry


class SequenceExecutor(ActionExecutor):
    def __init__(self):
        self.executed = []

    def execute(self, action, timeout):
        self.executed.append(action["name"])
        if action.get("fail"):
            raise RuntimeError("vendor rejected action")
        return object()  # Completion, not the return value, is the contract.


def test_action_runner_reports_completion_and_execution_error():
    executor = SequenceExecutor()
    runner = ActionRunner(executor, max_workers=1)
    try:
        result = runner.submit(
            "RULE",
            (
                {"type": "vendor", "name": "first"},
                {"type": "vendor", "name": "second", "fail": True},
                {"type": "vendor", "name": "not-run"},
            ),
            1,
        ).result(timeout=1)
    finally:
        runner.shutdown()

    assert executor.executed == ["first", "second"]
    assert result.state == "EXECUTION_ERROR"
    assert [item.status for item in result.actions] == [
        "COMPLETED",
        "EXECUTION_ERROR",
    ]
    assert result.last_error == "vendor rejected action"


def test_action_runner_reports_timeout_without_waiting_for_vendor_return():
    started = threading.Event()
    release = threading.Event()

    class BlockingExecutor(ActionExecutor):
        def execute(self, action, timeout):
            started.set()
            release.wait()

    runner = ActionRunner(BlockingExecutor(), max_workers=1)
    try:
        result = runner.submit(
            "RULE", ({"type": "vendor", "timeout": 0.01},), None
        ).result(timeout=1)
        assert started.is_set()
        assert result.state == "TIMED_OUT"
        assert result.actions[0].status == "TIMED_OUT"
    finally:
        release.set()
        runner.shutdown()


def test_artifact_request_is_immediate_and_final_file_is_atomic(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("bounded log", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def query(unused):
        started.set()
        release.wait(1)
        return "diagnostic output"

    client = FilesystemArtifactClient(
        directory=str(tmp_path / "artifacts"),
        query_runner=query,
        max_workers=1,
        max_artifact_bytes=4096,
    )
    try:
        reference = client.request(
            {"rule": "TEST"}, (str(log),), ({"type": "vendor"},)
        )
        assert started.wait(1)
        assert reference.artifact_id.startswith("dldd-")
        assert reference.location.endswith(reference.artifact_id)
        assert not Path(reference.location).exists()
        assert not list((tmp_path / "artifacts").glob("*.json"))

        release.set()
        deadline = time.time() + 2
        while not Path(reference.location).exists() and time.time() < deadline:
            time.sleep(0.01)

        with tarfile.open(reference.location, "r:gz") as archive:
            assert json.load(archive.extractfile("metadata.json")) == {
                "rule": "TEST"
            }
            assert archive.extractfile("queries/000.txt").read() == b"diagnostic output"
            assert "logs/service.log" in archive.getnames()
    finally:
        release.set()
        client.shutdown()


class I2CBusHook(VendorHook):
    def collect(self, operation):
        return None

    def execute_action(self, action):
        return None

    def resolve_i2c_bus(self, bus, operation):
        return {"MUX-A": "6", "MUX-B": "7"}[bus]


def test_i2c_action_uses_vendor_logical_bus_resolution(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"0x01\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    hooks = VendorHookRegistry()
    hooks.register("i2c", I2CBusHook())
    output = ActionExecutor(hooks=hooks)._execute_i2c(
        {
            "path": {
                "i2c_type": "get",
                "bus": ["MUX-A", "MUX-B"],
                "chip_addr": "0x58",
                "command": "0x7A",
                "size": "b",
            }
        },
        5,
    )

    assert output == ["0x01", "0x01"]
    assert [argv[3] for argv in calls] == ["6", "7"]
