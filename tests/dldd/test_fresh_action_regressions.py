import json
import os
import sys
import threading
import time

import pytest

from dldd.actions import ActionExecutor, ActionOutput, ActionRunner
from dldd.models import Operation


def test_real_cli_timeout_keeps_bounded_output_after_cleanup(tmp_path, monkeypatch):
    original_killpg = os.killpg
    cleanup_started = threading.Event()

    def delayed_killpg(pid, signal):
        cleanup_started.set()
        time.sleep(0.05)
        original_killpg(pid, signal)

    monkeypatch.setattr("dldd.command_execution.os.killpg", delayed_killpg)
    marker = tmp_path / "later-action-ran"
    limit = 16
    actions = (
        {
            "type": "cli", "timeout": 1, "max_output_bytes": limit,
            "argv": [sys.executable, "-c", (
                "import sys,time; print('stdout-before-timeout',flush=True); "
                "print('stderr-before-timeout',file=sys.stderr,flush=True); "
                "time.sleep(5)"
            )],
        },
        {
            "type": "cli", "timeout": 1,
            "argv": [sys.executable, "-c", (
                "from pathlib import Path; Path({!r}).touch()".format(str(marker))
            )],
        },
    )
    runner = ActionRunner(ActionExecutor(), max_workers=1)
    try:
        result = runner.submit("TEST", actions, None).result(timeout=3)
    finally:
        runner.shutdown()

    assert cleanup_started.is_set()
    assert result.state == "TIMED_OUT"
    assert len(result.actions) == 1
    assert not marker.exists()
    action = result.actions[0]
    assert action.error == "action timed out after 1 seconds"
    assert action.returncode is None
    artifact = action.as_artifact_payload(0)
    assert artifact["stdout"] == "stdout-before-timeout\n"[:limit]
    assert artifact["stderr"] == "stderr-before-timeout\n"[:limit]
    assert artifact["truncated"] == ["stdout", "stderr"]
    routine = json.dumps(action.as_payload())
    assert "stdout-before" not in routine
    assert "stderr-before" not in routine
    assert set(action.as_payload()) == {
        "type", "status", "started_at", "completed_at", "error",
    }


@pytest.mark.parametrize("action_type", ["vendor_repair", "dse", "cli"])
def test_late_action_callback_stays_bounded_and_retains_call_slot(action_type):
    entered = threading.Event()
    release = threading.Event()
    threads = []

    def callback(_operation):
        threads.append(threading.current_thread())
        entered.set()
        release.wait(2)
        return ActionOutput(stdout="late-output")

    action = {
        **Operation(type=action_type, executor=callback).as_runtime_payload(),
        "timeout": 0.01,
    }
    runner = ActionRunner(ActionExecutor(), max_workers=1)
    try:
        result = runner.submit("TEST", (action, action), None).result(timeout=0.5)
        assert entered.is_set()
        assert result.state == "TIMED_OUT"
        assert len(result.actions) == 1
        assert result.actions[0].stdout == ""

        blocked = runner.submit("TEST", (action,), None).result(timeout=0.5)
        assert blocked.state == "EXECUTION_ERROR"
        assert "capacity is exhausted" in blocked.last_error
        assert len(threads) == 1

        release.set()
        threads[0].join(timeout=0.5)
        assert not threads[0].is_alive()
        recovered = runner.submit(
            "TEST", ({**action, "timeout": 1},), None
        ).result(timeout=0.5)
        assert recovered.state == "COMPLETED"
        assert recovered.actions[0].stdout == "late-output"
    finally:
        release.set()
        runner.shutdown()
        for thread in threads:
            thread.join(timeout=0.5)
