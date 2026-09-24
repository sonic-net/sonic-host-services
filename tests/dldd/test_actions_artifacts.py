from pathlib import Path
import json
import subprocess
import tarfile
import threading
import time

from dldd.actions import ActionExecutor, ActionOutput, ActionRunner
from dldd.artifacts import FilesystemArtifactClient
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import Operation


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


def test_dse_and_cli_action_outputs_are_bounded_and_archived(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 7, stdout=b"cli-output", stderr=b"cli-warning"
        )

    monkeypatch.setattr(subprocess, "run", run)
    dse_action = Operation(
        type="dse",
        command="test:repair()",
        timeout=1,
        executor=lambda _unused: ActionOutput(
            stdout="dse-stdout",
            stderr="dse-stderr",
            result={"changed": True},
        ),
    ).as_runtime_payload()
    cli_action = {
        "type": "cli",
        "argv": ["diagnostic"],
        "timeout": 1,
        "max_output_bytes": 4,
    }
    runner = ActionRunner(ActionExecutor(), max_workers=1)
    try:
        result = runner.submit(
            "RULE", (dse_action, cli_action), None
        ).result(timeout=1)
    finally:
        runner.shutdown()

    assert result.state == "EXECUTION_ERROR"
    assert result.actions[0].as_payload() == {
        "type": "dse",
        "status": "COMPLETED",
        "started_at": int(result.actions[0].started_at),
        "completed_at": int(result.actions[0].completed_at),
    }
    assert result.actions[1].returncode == 7
    assert result.actions[1].stdout == "cli-"
    assert result.actions[1].stderr == "cli-"
    assert result.actions[1].truncated == ("stdout", "stderr")

    client = FilesystemArtifactClient(directory=str(tmp_path / "artifacts"))
    try:
        reference = client.request(
            {
                "rule": "TEST",
                "action_outputs": tuple(
                    action.as_artifact_payload(index)
                    for index, action in enumerate(result.actions)
                ),
            },
            (),
            (),
        )
        deadline = time.time() + 2
        while not Path(reference.location).exists() and time.time() < deadline:
            time.sleep(0.01)

        with tarfile.open(reference.location, "r:gz") as archive:
            assert archive.extractfile("actions/000/stdout.txt").read() == b"dse-stdout"
            assert archive.extractfile("actions/000/stderr.txt").read() == b"dse-stderr"
            assert archive.extractfile("actions/000/result.txt").read() == (
                b'{"changed": true}'
            )
            assert archive.extractfile("actions/001/stdout.txt").read() == b"cli-"
            assert archive.extractfile("actions/001/stderr.txt").read() == b"cli-"
            cli_metadata = json.load(
                archive.extractfile("actions/001/metadata.json")
            )
            assert cli_metadata["returncode"] == 7
            assert cli_metadata["truncated"] == ["stdout", "stderr"]
    finally:
        client.shutdown()


def test_artifact_request_is_immediate_and_final_file_is_atomic(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("bounded log", encoding="utf-8")
    other_log = tmp_path / "nested" / "service.log"
    other_log.parent.mkdir()
    other_log.write_text("same basename", encoding="utf-8")
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
            {"rule": "TEST"},
            (str(log), str(tmp_path / "*.log"), str(other_log)),
            ({"type": "vendor"},),
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
            log_member = "logs/{}".format(str(log).lstrip("/"))
            other_member = "logs/{}".format(str(other_log).lstrip("/"))
            assert json.load(archive.extractfile("metadata.json")) == {
                "rule": "TEST"
            }
            assert archive.extractfile("queries/000.txt").read() == b"diagnostic output"
            assert archive.getnames().count(log_member) == 1
            assert archive.extractfile(log_member).read() == b"bounded log"
            assert archive.extractfile(other_member).read() == b"same basename"
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
