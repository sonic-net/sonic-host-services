"""Focused host Healthz stream, projection, and D-Bus contract tests."""

import errno
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

try:
    import dbus
except ImportError:
    dbus = types.ModuleType("dbus")
    service = types.ModuleType("dbus.service")
    service.Object = object
    service.method = lambda *args, **kwargs: lambda function: function
    service.signal = service.method
    service.BusName = lambda *args, **kwargs: object()
    dbus.service = service
    dbus.SystemBus = lambda: object()
    sys.modules["dbus"] = dbus
    sys.modules["dbus.service"] = service


healthz = importlib.import_module("host_modules.healthz")
HealthzCatalog = importlib.import_module("host_modules.healthz_catalog").HealthzCatalog


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def hset(self, key, mapping):
        self.commands.append(("hset", key, mapping))
        return self

    def hdel(self, key, field):
        self.commands.append(("hdel", key, field))
        return self

    def execute(self):
        if self.redis.fail_projection:
            self.redis.fail_projection = False
            raise OSError("projection unavailable")
        for command, key, value in self.commands:
            if command == "hset":
                self.redis.rows.setdefault(key, {}).update(value)
            else:
                self.redis.rows.setdefault(key, {}).pop(value, None)
        return []


class FakeRedis:
    def __init__(self):
        self.stream = []
        self.rows = {}
        self.fail_projection = False
        self.on_scan = None
        self.run_id = "redis-1"

    def info(self, section):
        assert section == "server"
        return {"run_id": self.run_id}

    def append(self, stream_id, **values):
        self.stream.append((stream_id.encode(), {
            key.encode(): str(value).encode() for key, value in values.items()
        }))

    def xrange(self, _key, count):
        return self.stream[:count]

    def xrevrange(self, _key, count):
        return list(reversed(self.stream[-count:]))

    def xread(self, streams, count, block=None):
        checkpoint = next(iter(streams.values()))
        newer = [entry for entry in self.stream
                 if healthz._stream_id(entry[0]) > healthz._stream_id(checkpoint)]
        return [(healthz.TRANSITION_STREAM, newer[:count])] if newer else []

    def scan_iter(self, match):
        if self.on_scan is not None:
            callback, self.on_scan = self.on_scan, None
            callback(match)
        prefix = match[:-1]
        return iter(key.encode() for key in sorted(self.rows) if key.startswith(prefix))

    def hgetall(self, key):
        row = self.rows[healthz._text(key)]
        return {name.encode(): str(value).encode() for name, value in row.items()}

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)


def transition(component, symptom, status, observed_at, artifact_id=None):
    result = {
        "producer": "dldd", "fault_key": f"FAULT_INFO|{component}|{symptom}",
        "component": component, "component_type": "PSU", "symptom": symptom,
        "status": status, "occurrence": "1", "observed_at": str(observed_at),
    }
    if artifact_id:
        result["artifact_id"] = artifact_id
    return result


class TestHealthzWorker(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = HealthzCatalog(os.path.join(self.directory.name, "catalog.sqlite3"))
        self.redis = FakeRedis()
        self.worker = healthz.HealthzWorker(
            self.catalog, healthz.HealthzRedis(self.redis), reconcile_seconds=60
        )

    def tearDown(self):
        self.catalog.close()
        self.directory.cleanup()

    def test_replay_publishes_aggregate_without_a_gnoi_call(self):
        self.redis.append("1-0", **transition("PSU0", "temperature", "ACTIVE", 100))
        self.worker.poll_once()
        self.assertEqual(self.catalog.get_checkpoint(), "1-0")
        self.assertEqual(self.redis.rows["COMPONENT_HEALTH_INFO|PSU0"], {
            "status": "UNHEALTHY", "unhealthy_count": "1",
            "last_unhealthy": "100000000000",
        })
        self.redis.append("2-0", **transition("PSU0", "temperature", "INACTIVE", 120))
        self.worker.poll_once()
        self.assertEqual(self.catalog.get_latest("PSU0")["status"], "HEALTHY")
        self.assertEqual(self.redis.rows["COMPONENT_HEALTH_INFO|PSU0"], {
            "status": "HEALTHY", "unhealthy_count": "1",
            "last_unhealthy": "100000000000",
        })

    def test_projection_retries_from_catalog(self):
        self.redis.append("1-0", **transition("PSU0", "alarm", "ACTIVE", 100))
        self.redis.fail_projection = True
        with self.assertRaises(OSError):
            self.worker.poll_once()
        self.assertEqual(self.catalog.get_checkpoint(), "1-0")
        self.worker.poll_once()
        self.assertEqual(self.redis.rows["COMPONENT_HEALTH_INFO|PSU0"]["status"],
                         "UNHEALTHY")

    def test_trimmed_stream_gap_keeps_known_active_state(self):
        self.catalog.apply_transition("1-0", transition("PSU0", "alarm", "ACTIVE", 100))
        self.redis.append("3-0", **transition("FAN0", "stalled", "ACTIVE", 110))
        self.worker.poll_once()
        self.assertIsNotNone(self.catalog.get_gap())
        self.assertEqual(self.catalog.get_aggregate("PSU0")["status"], "UNHEALTHY")
        self.assertEqual(self.catalog.get_latest("FAN0")["status"], "UNHEALTHY")

    def test_lost_stream_keeps_known_state_and_reprojects_after_restart(self):
        self.catalog.apply_transition("10-0", transition("PSU0", "alarm", "ACTIVE", 100))
        self.worker.poll_once()
        self.assertIn("lost or reset", self.catalog.get_gap()["reason"])
        self.assertEqual(self.catalog.get_checkpoint(), "0-0")
        self.assertEqual(self.catalog.get_aggregate("PSU0")["status"], "UNHEALTHY")
        self.assertEqual(self.redis.rows["COMPONENT_HEALTH_INFO|PSU0"]["status"],
                         "UNHEALTHY")

    def test_redis_restart_reports_possible_lost_tail_with_unchanged_bounds(self):
        record = transition("PSU0", "alarm", "ACTIVE", 100)
        self.catalog.apply_transition("10-0", record)
        self.catalog.set_redis_run_id("redis-before")
        # The old server could have held an unread 11-0 transition.  After
        # restart, the visible stream still ends at the persisted checkpoint.
        self.redis.append("10-0", **record)
        self.redis.run_id = "redis-after"
        self.worker.poll_once()
        self.assertEqual(self.catalog.get_checkpoint(), "10-0")
        self.assertIn("Redis run_id changed", self.catalog.get_gap()["reason"])
        self.assertEqual(self.catalog.get_redis_run_id(), "redis-after")
        self.assertEqual(self.catalog.get_aggregate("PSU0")["status"], "UNHEALTHY")

    def test_run_id_gap_write_failure_retries_before_persisting_new_epoch(self):
        self.catalog.set_redis_run_id("redis-before")
        self.redis.run_id = "redis-after"
        original = self.catalog.mark_gap
        attempts = [0]

        def fail_once(*args, **kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("SQLite busy")
            return original(*args, **kwargs)

        with mock.patch.object(self.catalog, "mark_gap", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.worker.poll_once()
            self.assertEqual(self.catalog.get_redis_run_id(), "redis-before")
            self.worker.poll_once()
        self.assertEqual(self.catalog.get_redis_run_id(), "redis-after")
        self.assertEqual(attempts[0], 2)

    def test_snapshot_observation_is_deduplicated_and_not_a_lost_history_claim(self):
        self.redis.rows["FAULT_INFO|PSU0|alarm"] = {
            "producer": "dldd", "component_name": "PSU0", "symptom": "alarm",
            "status": "ACTIVE", "occurrences": "1", "last_detection_time": "100",
        }
        self.worker.poll_once()
        event = self.catalog.get_latest("PSU0")
        self.assertEqual(event["status"], "UNHEALTHY")
        self.assertEqual(event["source"], "snapshot")
        self.assertIsNone(self.catalog.get_checkpoint())
        self.worker._next_reconcile = 0
        self.worker.poll_once()
        self.assertEqual(len(self.catalog.list_events("PSU0", True)), 1)

    def test_snapshot_defers_when_stream_tail_moves_during_scan(self):
        self.redis.rows["FAULT_INFO|PSU0|alarm"] = {
            "producer": "dldd", "component_name": "PSU0", "symptom": "alarm",
            "status": "ACTIVE", "occurrences": "1", "last_detection_time": "100",
        }
        artifact = "dldd-{}.tar.gz".format("a" * 32)
        self.redis.on_scan = lambda _pattern: self.redis.append(
            "1-0", **transition("PSU0", "alarm", "ACTIVE", 100, artifact))
        self.worker.poll_once()
        self.assertIsNone(self.catalog.get_latest("PSU0"))
        self.worker.poll_once()
        self.assertEqual(self.catalog.get_latest("PSU0")["id"], artifact)

    def test_published_physical_entity_parent_relation_is_cached(self):
        self.redis.rows["PHYSICAL_ENTITY_INFO|PSU0"] = {"parent_name": "chassis"}
        self.worker.poll_once()
        self.assertEqual(self.worker.relations.descendants(
            "chassis", ["PSU0", "FAN0"]), ["PSU0"])

    def test_invalid_stream_record_is_skipped_with_gap_and_next_record_runs(self):
        invalid = transition("PSU0", "alarm", "BROKEN", 100)
        self.redis.append("1-0", **invalid)
        self.redis.append("2-0", **transition("PSU0", "alarm", "ACTIVE", 101))
        self.worker.poll_once()
        self.assertEqual(self.catalog.get_checkpoint(), "2-0")
        self.assertIn("invalid transition", self.catalog.get_gap()["reason"])
        self.assertEqual(self.catalog.get_latest("PSU0")["status"], "UNHEALTHY")

    def test_gap_persistence_failure_retries_same_invalid_record(self):
        self.redis.append("1-0", **transition("PSU0", "alarm", "BROKEN", 100))
        self.redis.append("2-0", **transition("PSU0", "alarm", "ACTIVE", 101))
        original = self.catalog.mark_gap
        attempts = [0]

        def fail_once(*args, **kwargs):
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("SQLite busy")
            return original(*args, **kwargs)

        with mock.patch.object(self.catalog, "mark_gap", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.worker.poll_once()
            self.assertIsNone(self.catalog.get_checkpoint())
            self.worker.poll_once()
        self.assertEqual(self.catalog.get_checkpoint(), "2-0")
        self.assertEqual(self.catalog.get_latest("PSU0")["status"], "UNHEALTHY")

    def test_snapshot_updates_confirmation_time_without_new_event(self):
        self.redis.append("1-0", **transition("PSU0", "alarm", "ACTIVE", 100))
        self.worker.poll_once()
        events = len(self.catalog.list_events("PSU0", True))
        self.redis.rows["FAULT_INFO|PSU0|alarm"] = {
            "producer": "dldd", "component_name": "PSU0", "symptom": "alarm",
            "status": "ACTIVE", "occurrences": "1", "last_detection_time": "130",
        }
        self.worker._next_reconcile = 0
        self.worker.poll_once()
        self.assertEqual(len(self.catalog.list_events("PSU0", True)), events)
        self.assertEqual(self.redis.rows["COMPONENT_HEALTH_INFO|PSU0"]["last_unhealthy"],
                         "130000000000")


class TestHealthzDbus(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.catalog = HealthzCatalog(os.path.join(self.directory.name, "catalog.sqlite3"))
        self.artifacts = os.path.join(self.directory.name, "artifacts")
        os.makedirs(self.artifacts)
        with mock.patch.object(healthz.host_service.HostModule, "__init__", return_value=None):
            self.endpoint = healthz.Healthz(catalog=self.catalog,
                                            state_db=healthz.HealthzRedis(FakeRedis()),
                                            artifact_directory=self.artifacts,
                                            start_worker=False)

    def tearDown(self):
        self.catalog.close()
        self.directory.cleanup()

    def test_get_list_ack_are_quick_catalog_reads_and_preserve_archive(self):
        artifact = "dldd-{}{}.tar.gz".format("a" * 16, "b" * 16)
        self.catalog.apply_transition("1-0", transition("PSU0", "alarm", "ACTIVE", 100,
                                                        artifact))
        archive = os.path.join(self.artifacts, artifact)
        with open(archive, "wb") as output:
            output.write(b"archive")
        code, body = self.endpoint.get('{"component":"PSU0"}')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(body)["id"], artifact)
        self.assertEqual(json.loads(body)["artifact_id"], artifact)
        code, body = self.endpoint.ack(json.dumps({"component": "PSU0", "id": artifact}))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(body)["acknowledged"])
        self.assertTrue(os.path.isfile(archive))
        self.assertEqual(json.loads(self.endpoint.list("{}")[1]), [])
        self.assertEqual(len(json.loads(self.endpoint.list(
            '{"include_acknowledged":true}')[1])), 1)
        self.assertEqual(self.endpoint.ack(json.dumps({"component": "PSU0",
                                                       "id": artifact}))[0], 0)
        os.unlink(archive)
        self.assertNotIn("artifact_id", json.loads(self.endpoint.get(
            '{"component":"PSU0"}')[1]))

    def test_get_includes_only_published_descendants(self):
        self.catalog.apply_transition("1-0", transition("chassis", "alarm", "ACTIVE", 100))
        self.catalog.apply_transition("2-0", transition("PSU0", "alarm", "ACTIVE", 101))
        self.catalog.apply_transition("3-0", transition("FAN0", "alarm", "ACTIVE", 102))
        self.endpoint.relations.replace({"PSU0": "chassis"})
        body = json.loads(self.endpoint.get('{"component":"chassis"}')[1])
        self.assertEqual([child["component"] for child in body["children"]], ["PSU0"])
        self.endpoint.relations.replace({})
        self.assertNotIn("children", json.loads(self.endpoint.get(
            '{"component":"chassis"}')[1]))

    def test_parent_without_own_event_is_an_unassessed_wrapper(self):
        self.catalog.apply_transition("1-0", transition("PSU0", "alarm", "ACTIVE", 100))
        self.endpoint.relations.replace({"PSU0": "chassis"})
        code, body = self.endpoint.get('{"component":"chassis"}')
        self.assertEqual(code, 0)
        wrapper = json.loads(body)
        self.assertEqual(wrapper["status"], "UNSPECIFIED")
        self.assertEqual(wrapper["children"][0]["component"], "PSU0")
        self.assertNotIn("id", wrapper)
        self.assertNotIn("observed_at", wrapper)
        self.endpoint.relations.replace({})
        self.assertEqual(self.endpoint.get('{"component":"chassis"}')[0],
                         errno.ENOENT)

    def test_parent_keeps_published_nested_hierarchy(self):
        self.catalog.apply_transition("1-0", transition("PSU0", "alarm", "ACTIVE", 100))
        self.endpoint.relations.replace({"slot0": "chassis", "PSU0": "slot0"})
        body = json.loads(self.endpoint.get('{"component":"chassis"}')[1])
        self.assertEqual(body["children"][0]["component"], "slot0")
        self.assertEqual(body["children"][0]["status"], "UNSPECIFIED")
        self.assertEqual(body["children"][0]["children"][0]["component"], "PSU0")

    def test_event_id_allows_pending_artifact_wait_without_advertising_missing_file(self):
        artifact = "dldd-{}.tar.gz".format("c" * 32)
        self.catalog.apply_transition("1-0", transition("PSU0", "alarm", "ACTIVE",
                                                        1000, artifact))
        body = json.loads(self.endpoint.get('{"component":"PSU0"}')[1])
        self.assertEqual(body["id"], artifact)
        self.assertNotIn("artifact_id", body)
        archive = os.path.join(self.artifacts, artifact)
        with open(archive, "wb") as output:
            output.write(b"archive")
        body = json.loads(self.endpoint.get('{"component":"PSU0"}')[1])
        self.assertEqual(body["artifact_id"], artifact)
        os.unlink(archive)
        body = json.loads(self.endpoint.get('{"component":"PSU0"}')[1])
        self.assertNotIn("artifact_id", body)

    def test_validation_and_not_found(self):
        self.assertEqual(self.endpoint.get('{"component":"missing"}')[0], errno.ENOENT)
        self.assertEqual(self.endpoint.ack('{"component":"PSU0"}')[0], errno.EINVAL)
        self.assertEqual(self.endpoint.list('{"include_acknowledged":"yes"}')[0],
                         errno.EINVAL)


if __name__ == "__main__":
    unittest.main()
