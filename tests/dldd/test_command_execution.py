import fcntl
import json
import subprocess
import sys
import time
import tracemalloc

import pytest

from dldd.actions import ActionExecutor, ActionRunner
from dldd.command_execution import run_shell_free


def test_real_command_drains_both_streams_with_bounded_capture_memory():
    script = (
        "import sys\n"
        "for _ in range(128):\n"
        " sys.stdout.buffer.write(b'o' * 65536)\n"
        " sys.stderr.buffer.write(b'e' * 65536)\n"
    )
    tracemalloc.start()
    try:
        result = run_shell_free(
            [sys.executable, "-c", script], timeout=10, max_output_bytes=1024
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert result.returncode == 0
    assert result.stdout == b"o" * 1024
    assert result.stderr == b"e" * 1024
    assert result.stdout_truncated and result.stderr_truncated
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("parent_mode", ["running", "exited"])
def test_real_command_timeout_cleans_up_descendants(tmp_path, parent_mode):
    lock_path = tmp_path / "descendant.lock"
    script = """
import fcntl, os, sys, time

with open(sys.argv[1], "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if os.fork() == 0:
        time.sleep(20)
        os._exit(0)
    print("started", flush=True)
    print("diagnostic", file=sys.stderr, flush=True)
    if sys.argv[2] == "running":
        time.sleep(20)
"""
    with pytest.raises(subprocess.TimeoutExpired) as error:
        run_shell_free(
            [sys.executable, "-c", script, str(lock_path), parent_mode], timeout=2
        )
    assert error.value.stdout == b"started\n"
    assert error.value.stderr == b"diagnostic\n"

    # The descendant inherits the lock and releases it when killed.
    deadline = time.monotonic() + 2
    with lock_path.open("r+") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail("descendant still holds the lock after timeout")
                time.sleep(0.02)


@pytest.mark.parametrize("action_type", ["cli", "i2c"])
def test_failed_command_action_keeps_output_out_of_routine_telemetry(tmp_path, action_type):
    script = (
        "import sys; print('stdout-marker'); "
        "sys.stderr.write('stderr-marker'); sys.exit(7)"
    )
    if action_type == "cli":
        action = {"type": "cli", "argv": [sys.executable, "-c", script], "timeout": 5}
    else:
        tool = tmp_path / "i2c-tool"
        tool.write_text("#!" + sys.executable + "\n" + script + "\n")
        tool.chmod(0o700)
        action = {"type": "i2c", "timeout": 5, "path": {
            "executable": str(tool), "bus": "6", "chip_addr": "0x58",
            "i2c_type": "get", "command": "0x7a", "size": "b"}}
    runner = ActionRunner(ActionExecutor(), max_workers=1)
    try:
        result = runner.submit(
            "TEST",
            (action,),
            None,
        ).result(timeout=10)
    finally:
        runner.shutdown()

    assert result.state == "EXECUTION_ERROR"
    assert result.last_error == action_type.upper() + " action exited 7"
    routine = json.dumps(result.actions[0].as_payload())
    assert "stdout-marker" not in routine and "stderr-marker" not in routine
    artifact = result.actions[0].as_artifact_payload(0)
    assert artifact["stdout"] == "stdout-marker\n"
    assert artifact["stderr"] == "stderr-marker"
    assert artifact["returncode"] == 7
