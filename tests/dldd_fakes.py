"""Shared Redis-accurate test doubles for DLDD suites."""

from __future__ import annotations

from copy import deepcopy
import fnmatch
import json
from pathlib import Path
from threading import RLock

from dldd.telemetry import StateDB, _redis_value


_VALID_RULE_FIXTURE = (
    Path(__file__).parent / "dldd" / "fixtures" / "valid-redis-rule.json"
)


def load_valid_rules_document():
    """Return a fresh mutable copy of the canonical valid rules fixture."""

    return json.loads(_VALID_RULE_FIXTURE.read_text(encoding="utf-8"))


def valid_rule_signature(document, index=0):
    """Return a mutable signature mapping from a canonical rules document."""

    return document["signatures"][index]["signature"]


def valid_rule_event(document, rule_index=0, event_index=0):
    """Return a mutable event mapping from a canonical rules document."""

    return valid_rule_signature(document, rule_index)["conditions"]["events"][
        event_index
    ]["event"]


def valid_rule_action(document, rule_index=0, action_index=0):
    """Return a mutable local-action mapping from a canonical rules document."""

    actions = valid_rule_signature(document, rule_index)["actions"]
    return actions["repair_actions"]["local_actions"]["action_list"][
        action_index
    ]["action"]


def append_rule(document, *, name, rule_id, source_index=0):
    """Append and return a uniquely identified copy of an existing rule."""

    wrapper = deepcopy(document["signatures"][source_index])
    metadata = wrapper["signature"]["metadata"]
    metadata["name"] = name
    metadata["id"] = rule_id
    document["signatures"].append(wrapper)
    return wrapper["signature"]


class FakeStateDB(StateDB):
    """In-memory hash/TTL store with Redis HSET merge semantics."""

    def __init__(self, values=None):
        self.values = {} if values is None else values
        self.ttls = {}
        self.delete_calls = 0
        self.read_error = None
        self.write_error = None
        self.read_failures = 0
        self.write_failures = 0
        self._lock = RLock()

    def fail_reads_with(self, error):
        """Fail database reads until explicitly cleared by the test."""

        self.read_error = error

    def fail_writes_with(self, error):
        """Fail database mutations until explicitly cleared by the test."""

        self.write_error = error

    def clear_failures(self):
        self.read_error = None
        self.write_error = None

    def _check_read(self):
        if self.read_error is not None:
            self.read_failures += 1
            raise self.read_error

    def _check_write(self):
        if self.write_error is not None:
            self.write_failures += 1
            raise self.write_error

    def hset(self, key, values):
        with self._lock:
            self._check_write()
            current = self.values.setdefault(key, {})
            current.update(
                {name: _redis_value(value) for name, value in values.items()}
            )

    def expire(self, key, seconds):
        with self._lock:
            self._check_write()
            self.ttls[key] = seconds

    def persist(self, key):
        with self._lock:
            self._check_write()
            self.ttls.pop(key, None)

    def hdel(self, key, fields):
        with self._lock:
            self._check_write()
            for field in fields:
                self.values.get(key, {}).pop(field, None)

    def delete(self, key):
        with self._lock:
            self._check_write()
            self.delete_calls += 1
            self.values.pop(key, None)
            self.ttls.pop(key, None)

    def hgetall(self, key):
        with self._lock:
            self._check_read()
            return dict(self.values.get(key, {}))

    def keys(self, pattern):
        with self._lock:
            self._check_read()
            return [
                key for key in self.values if fnmatch.fnmatch(key, pattern)
            ]
