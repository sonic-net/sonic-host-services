"""Shared Redis-accurate test doubles for DLDD suites."""

from __future__ import annotations

import fnmatch

from dldd.telemetry import StateDB, _redis_value


class FakeStateDB(StateDB):
    """In-memory hash/TTL store with Redis HSET merge semantics."""

    def __init__(self, values=None):
        self.values = {} if values is None else values
        self.ttls = {}
        self.delete_calls = 0

    def hset(self, key, values):
        current = self.values.setdefault(key, {})
        current.update(
            {name: _redis_value(value) for name, value in values.items()}
        )

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def persist(self, key):
        self.ttls.pop(key, None)

    def hdel(self, key, fields):
        for field in fields:
            self.values.get(key, {}).pop(field, None)

    def delete(self, key):
        self.delete_calls += 1
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def hgetall(self, key):
        return self.values.get(key, {})

    def keys(self, pattern):
        return [
            key for key in self.values if fnmatch.fnmatch(key, pattern)
        ]
