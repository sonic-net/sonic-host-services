from pathlib import Path
import json
import subprocess
import tarfile
import threading

from dldd.actions import ActionExecutor, ActionOutput, ActionRunner
from dldd.artifacts import HostHealthzArtifactClient
from dldd.hooks import VendorHook, VendorHookRegistry
from dldd.models import Operation
from host_modules.healthz_artifacts import HealthzArtifacts


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


class RecordingHealthz:
    def __init__(self, tmp_path):
        self.location = str(tmp_path / "healthz-artifact.tar.gz")
        self.submitted = []
        self.failed = []
        self.stage_paths = []

    def __call__(self, method, request):
        if method == "reserve_artifact":
            return {
                "artifact_id": "healthz-123.tar.gz",
                "requested_at": 1234,
                "location": self.location,
            }
        if method == "submit_artifact":
            self.stage_paths = [Path(entry["path"]) for entry in request["paths"]]
            files = {
                entry["name"]: Path(entry["path"]).read_bytes()
                for entry in request["paths"]
            }
            self.submitted.append((request["metadata"], files))
            return {"artifact_id": request["artifact_id"], "location": self.location}
        if method == "fail_artifact":
            self.failed.append(request["artifact_id"])
            return {}
        if method == "artifact_status":
            return {"state": "COMPLETED"}
        raise AssertionError(method)


def test_dse_and_cli_action_outputs_are_bounded_and_submitted(tmp_path, monkeypatch):
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

    healthz = RecordingHealthz(tmp_path)
    client = HostHealthzArtifactClient(healthz_call=healthz)
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
        client._jobs.join()
        assert reference.artifact_id == "healthz-123.tar.gz"
        metadata, files = healthz.submitted[0]
        assert metadata == {"rule": "TEST"}
        assert files["actions/000/stdout.txt"] == b"dse-stdout"
        assert files["actions/000/stderr.txt"] == b"dse-stderr"
        assert files["actions/000/result.txt"] == b'{"changed": true}'
        assert files["actions/001/stdout.txt"] == b"cli-"
        assert files["actions/001/stderr.txt"] == b"cli-"
        cli_metadata = json.loads(files["actions/001/metadata.json"])
        assert cli_metadata["returncode"] == 7
        assert cli_metadata["truncated"] == ["stdout", "stderr"]
        assert all(
            not path.exists() for path in healthz.stage_paths
            if "dldd-healthz-" in str(path)
        )
    finally:
        client.shutdown()


def test_artifact_id_is_immediate_and_submit_waits_for_queries(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("bounded log", encoding="utf-8")
    other_log = tmp_path / "nested" / "service.log"
    other_log.parent.mkdir()
    other_log.write_text("same basename", encoding="utf-8")
    linked_log = tmp_path / "link.log"
    linked_log.symlink_to(log)
    started = threading.Event()
    release = threading.Event()

    def query(unused):
        started.set()
        release.wait(1)
        return "diagnostic output"

    healthz = RecordingHealthz(tmp_path)
    client = HostHealthzArtifactClient(
        query_runner=query,
        healthz_call=healthz,
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
        assert reference.location == healthz.location
        assert healthz.submitted == []
        release.set()
        client._jobs.join()
        metadata, files = healthz.submitted[0]
        assert metadata == {"rule": "TEST"}
        assert files["queries/000.txt"] == b"diagnostic output"
        assert files["logs/" + str(log).lstrip("/")] == b"bounded log"
        assert files["logs/" + str(other_log).lstrip("/")] == b"same basename"
        assert len([name for name in files if name.endswith("service.log")]) == 2
        assert "logs/" + str(linked_log).lstrip("/") not in files
        assert all(
            not path.exists() for path in healthz.stage_paths
            if "dldd-healthz-" in str(path)
        )
        assert client.artifact_status(reference.artifact_id) == "COMPLETED"
    finally:
        release.set()
        client.shutdown()


def test_log_is_staged_before_healthz_opens_it(tmp_path):
    log = tmp_path / "service.log"
    log.write_bytes(b"before rotation")
    healthz = RecordingHealthz(tmp_path)

    def call(method, request):
        if method == "submit_artifact":
            log.unlink()
        return healthz(method, request)

    client = HostHealthzArtifactClient(healthz_call=call)
    try:
        client.request({"rule": "TEST"}, (str(log),), ())
        client._jobs.join()
        assert healthz.failed == []
        assert healthz.submitted[0][1][
            "logs/" + str(log).lstrip("/")
        ] == b"before rotation"
        assert all("dldd-healthz-" in str(path) for path in healthz.stage_paths)
    finally:
        client.shutdown()


def test_failed_dldd_collection_releases_healthz_reservation(tmp_path):
    def fail(_unused):
        raise RuntimeError("vendor query failed")

    healthz = RecordingHealthz(tmp_path)
    client = HostHealthzArtifactClient(query_runner=fail, healthz_call=healthz)
    try:
        reference = client.request({}, (), ({"type": "vendor"},))
        client._jobs.join()
        assert healthz.failed == [reference.artifact_id]
        assert healthz.submitted == []
    finally:
        client.shutdown()


def test_dldd_manifest_packages_through_host_healthz(tmp_path):
    store = HealthzArtifacts(tmp_path / "healthz", max_bytes=4096)
    log = tmp_path / "source.log"
    log.write_text("log data", encoding="utf-8")
    configured_log = Path(log.resolve())

    def call(method, request):
        if method == "reserve_artifact":
            return store.reserve()
        if method == "submit_artifact":
            return store.submit(request["artifact_id"], request["paths"],
                                request["metadata"])
        if method == "fail_artifact":
            store.fail(request["artifact_id"])
            return {}
        if method == "artifact_status":
            return {"state": store.status(request["artifact_id"])}
        raise AssertionError(method)

    client = HostHealthzArtifactClient(
        query_runner=lambda _query: "query data", healthz_call=call,
        max_artifact_bytes=4096,
    )
    try:
        reference = client.request({"rule": "TEST"}, (str(configured_log),),
                                   ({"type": "vendor"},))
        client._jobs.join()
        assert store.status(reference.artifact_id) == "COMPLETED"
        with tarfile.open(reference.location, "r:gz") as archive:
            assert json.load(archive.extractfile("metadata.json")) == {"rule": "TEST"}
            assert archive.extractfile("queries/000.txt").read() == b"query data"
            assert archive.extractfile(
                "logs/" + str(configured_log).lstrip("/")
            ).read() == b"log data"
    finally:
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
