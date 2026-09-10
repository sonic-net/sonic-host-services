"""Bounded daemon-thread calls for vendor operations that may outlive timeout."""

from __future__ import annotations

import threading
from concurrent.futures import Future, InvalidStateError
from typing import Any, Callable, Tuple


def start_daemon_workers(
    count: int, name_prefix: str, target: Callable[[], None]
) -> Tuple[threading.Thread, ...]:
    """Create and start a consistently named pool of daemon workers."""

    workers = tuple(
        threading.Thread(
            target=target,
            name="{}{}".format(name_prefix, index),
            daemon=True,
        )
        for index in range(max(1, int(count)))
    )
    for worker in workers:
        worker.start()
    return workers


class BoundedCallGate:
    """Limit concurrent calls even when a timed-out call keeps running."""

    def __init__(self, capacity: int, thread_name: str) -> None:
        self._slots = threading.BoundedSemaphore(max(1, int(capacity)))
        self._thread_name = thread_name

    def start(self, callback, capacity_error: str) -> Future:
        """Start one call, retaining its slot until the callback actually exits."""

        if not self._slots.acquire(False):
            raise RuntimeError(capacity_error)
        result: Future[Any] = Future()

        def invoke():
            try:
                try:
                    result.set_result(callback())
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
