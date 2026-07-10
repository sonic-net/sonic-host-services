"""Bounded daemon-thread calls for vendor operations that may outlive timeout."""

from __future__ import annotations

import threading
from concurrent.futures import Future, InvalidStateError


class BoundedCallGate:
    def __init__(self, capacity: int, thread_name: str) -> None:
        self._slots = threading.BoundedSemaphore(max(1, int(capacity)))
        self._thread_name = thread_name

    def start(self, callback, capacity_error: str) -> Future:
        if not self._slots.acquire(False):
            raise RuntimeError(capacity_error)
        result = Future()

        def invoke():
            try:
                value = callback()
                try:
                    result.set_result(value)
                except InvalidStateError:
                    pass
            except BaseException as error:
                try:
                    result.set_exception(error)
                except InvalidStateError:
                    pass
            finally:
                self._slots.release()

        threading.Thread(
            target=invoke,
            name=self._thread_name,
            daemon=True,
        ).start()
        return result
