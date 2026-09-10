"""Stable-inbox detection for the systemd rules watcher timer."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from typing import Any, Callable, Optional

from .filesystem import atomic_write_json, load_json_object
from .lifecycle import sha256_file
from .timestamps import floor_timestamp


class RulesWatcher:
    """Restart DLDD after a stable, newly accepted rule generation appears."""

    def __init__(
        self,
        inbox_path: str,
        lock_path: str,
        state_path: str,
        settle_time: int = 30,
        restart: Optional[Callable[[], Any]] = None,
        clock=time.time,
    ) -> None:
        self.inbox_path = inbox_path
        self.lock_path = lock_path
        self.state_path = state_path
        self.settle_time = settle_time
        self.restart = restart or self._restart_service
        self.clock = clock

    @staticmethod
    def _restart_service() -> None:
        subprocess.run(
            ["/bin/systemctl", "--no-block", "restart", "dldd.service"],
            shell=False,
            check=True,
        )

    def check_once(self) -> bool:
        if not os.path.isfile(self.inbox_path):
            return False
        stat = os.stat(self.inbox_path)
        if stat.st_size <= 0:
            return False
        now = self.clock()
        state = load_json_object(self.state_path)
        observation = {"size": stat.st_size, "mtime": stat.st_mtime}
        previous = state.get("observation", {})
        first_seen = state.get("first_seen", now)
        if previous != observation:
            state.update({"observation": observation, "first_seen": now})
            atomic_write_json(self.state_path, state)
            return False
        if now - first_seen < self.settle_time:
            return False

        checksum = sha256_file(self.inbox_path)
        if checksum == state.get("last_restart_checksum"):
            return False

        # Release the activation lock before invoking systemctl.
        os.makedirs(os.path.dirname(self.lock_path), mode=0o755, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state["last_restart_checksum"] = checksum
            state["last_restart_requested_at"] = floor_timestamp(now)
            atomic_write_json(self.state_path, state)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            self.restart()
        except Exception as error:
            state.pop("last_restart_checksum", None)
            state["last_restart_error"] = str(error)
            atomic_write_json(self.state_path, state)
            raise
        return True
