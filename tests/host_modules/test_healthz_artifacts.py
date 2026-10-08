"""Generic host Healthz archive lifecycle and path safety."""

import os
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from host_modules import healthz_artifacts
from host_modules.healthz_artifacts import HealthzArtifacts, PENDING_SECONDS


class TestHealthzArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.directory = os.path.join(self.root, "artifacts")
        self.store = HealthzArtifacts(self.directory, max_artifacts=2,
                                      max_bytes=4096)

    def tearDown(self):
        self.temp.cleanup()

    def test_reserve_submit_restart_and_failure_keeps_complete_archive(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("diagnostic data")
        result = self.store.reserve()
        artifact_id = result["artifact_id"]
        self.assertEqual(self.store.status(artifact_id), "PENDING")
        reopened = HealthzArtifacts(self.directory, max_artifacts=2,
                                    max_bytes=4096)
        self.assertEqual(reopened.status(artifact_id), "PENDING")
        result = reopened.submit(artifact_id, [{"path": source,
                                                 "name": "logs/log.txt"}],
                                 {"source": "test"})
        self.assertEqual(reopened.status(artifact_id), "COMPLETED")
        with tarfile.open(result["location"], "r:gz") as archive:
            self.assertEqual(archive.extractfile("logs/log.txt").read(),
                             b"diagnostic data")
            self.assertIn(b"test", archive.extractfile("metadata.json").read())
        reopened.fail(artifact_id)
        self.assertEqual(reopened.status(artifact_id), "COMPLETED")
        self.assertTrue(os.path.isfile(result["location"]))
        self.assertEqual(reopened.submit(artifact_id, []), result)

    def test_rejects_unsafe_source_or_name_without_publishing(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("x")
        link = os.path.join(self.root, "link.txt")
        os.symlink(source, link)
        artifact_id = self.store.reserve()["artifact_id"]
        for item in ({"path": source, "name": "../escape"},
                     {"path": link, "name": "logs/link.txt"}):
            with self.assertRaises((ValueError, OSError)):
                self.store.submit(artifact_id, [item])
            self.assertEqual(self.store.status(artifact_id), "PENDING")
        self.store.fail(artifact_id)
        self.assertEqual(self.store.status(artifact_id), "MISSING")

    def test_rejects_source_with_symlinked_parent(self):
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        source = os.path.join(real, "log.txt")
        with open(source, "w") as output:
            output.write("private")
        alias = os.path.join(self.root, "alias")
        os.symlink(real, alias)
        artifact_id = self.store.reserve()["artifact_id"]

        with self.assertRaises((ValueError, OSError)):
            self.store.submit(artifact_id, [{
                "path": os.path.join(alias, "log.txt"), "name": "logs/log.txt",
            }])

        self.assertEqual(self.store.status(artifact_id), "PENDING")
        self.assertFalse(os.path.exists(self.store._archive(artifact_id)))

    def test_size_limit_and_bounded_retention(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "wb") as output:
            output.write(b"x" * 100)
        small = HealthzArtifacts(self.directory, max_artifacts=2, max_bytes=10)
        failed = small.reserve()["artifact_id"]
        with self.assertRaisesRegex(ValueError, "size limit"):
            small.submit(failed, [{"path": source, "name": "log.txt"}])
        small.fail(failed)
        first = self.store.reserve()["artifact_id"]
        self.store.submit(first, [{"path": source, "name": "log.txt"}])
        os.utime(os.path.join(self.directory, first), (1, 1))
        second = self.store.reserve()["artifact_id"]
        self.store.submit(second, [{"path": source, "name": "log.txt"}])
        third = self.store.reserve()["artifact_id"]
        self.assertEqual(self.store.status(first), "MISSING")
        self.assertEqual(self.store.status(second), "COMPLETED")
        self.assertEqual(self.store.status(third), "PENDING")

    def test_rejected_reservation_does_not_evict_completed_archive(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("diagnostic data")
        larger = HealthzArtifacts(self.directory, max_artifacts=3,
                                  max_bytes=4096)
        completed = larger.reserve()["artifact_id"]
        larger.submit(completed, [{"path": source, "name": "log.txt"}])
        pending = [larger.reserve()["artifact_id"] for _ in range(2)]

        # A restarted process can have a lower limit than the artifacts it
        # inherited.  Rejecting a new reservation must preserve old archives.
        with patch.object(self.store, "acknowledged_artifacts") as acknowledged:
            with self.assertRaisesRegex(OSError, "capacity is exhausted"):
                self.store.reserve()
            acknowledged.assert_not_called()
        self.assertEqual(self.store.status(completed), "COMPLETED")
        for artifact_id in pending:
            self.assertEqual(self.store.status(artifact_id), "PENDING")

    def test_unknown_or_unacknowledged_archive_retention_falls_back_to_age(self):
        # Both an event without acknowledgement and an archive whose event was
        # pruned have no known acknowledgement, so neither gets preference.
        self.store.acknowledged_artifacts = lambda: set()
        first = self.store.reserve()["artifact_id"]
        self.store.submit(first, [])
        os.utime(self.store._archive(first), (1, 1))
        second = self.store.reserve()["artifact_id"]
        self.store.submit(second, [])
        third = self.store.reserve()["artifact_id"]
        self.assertEqual(self.store.status(first), "MISSING")
        self.assertEqual(self.store.status(second), "COMPLETED")
        self.assertEqual(self.store.status(third), "PENDING")

    def test_catalog_read_failure_does_not_evict_completed_archives(self):
        artifacts = [self.store.reserve()["artifact_id"]]
        self.store.submit(artifacts[0], [])
        artifacts.append(self.store.reserve()["artifact_id"])
        self.store.submit(artifacts[1], [])
        with patch.object(self.store, "acknowledged_artifacts",
                          side_effect=OSError("catalog unavailable")):
            with self.assertRaisesRegex(OSError, "catalog unavailable"):
                self.store.reserve()
        for artifact_id in artifacts:
            self.assertEqual(self.store.status(artifact_id), "COMPLETED")

    def test_abandoned_reservation_expires(self):
        artifact_id = self.store.reserve()["artifact_id"]
        marker = self.store._pending(artifact_id)
        old = time.time() - PENDING_SECONDS - 1
        os.utime(marker, (old, old))
        self.assertEqual(self.store.status(artifact_id), "MISSING")
        self.store.reserve()
        self.assertFalse(os.path.exists(marker))

    def test_crash_after_publish_does_not_double_count_archive(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("diagnostic data")
        first = self.store.reserve()["artifact_id"]
        self.store.submit(first, [{"path": source, "name": "log.txt"}])
        os.utime(self.store._archive(first), (1, 1))

        second = self.store.reserve()["artifact_id"]
        marker = self.store._pending(second)
        unlink = os.unlink

        def crash_before_marker_removal(path):
            if path == marker:
                raise OSError("simulated crash after archive publication")
            return unlink(path)

        with patch("host_modules.healthz_artifacts.os.unlink",
                   side_effect=crash_before_marker_removal):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.store.submit(second, [{"path": source, "name": "log.txt"}])
        self.assertEqual(self.store.status(second), "COMPLETED")
        self.assertTrue(os.path.exists(marker))

        reopened = HealthzArtifacts(self.directory, max_artifacts=2,
                                    max_bytes=4096)
        third = reopened.reserve()["artifact_id"]
        self.assertEqual(reopened.status(first), "MISSING")
        self.assertEqual(reopened.status(second), "COMPLETED")
        self.assertEqual(reopened.status(third), "PENDING")
        self.assertFalse(os.path.exists(marker))

    def test_concurrent_stores_do_not_replace_completed_archive(self):
        first_source = os.path.join(self.root, "first.txt")
        second_source = os.path.join(self.root, "second.txt")
        for path, content in ((first_source, "first"),
                              (second_source, "second")):
            with open(path, "w") as output:
                output.write(content)
        artifact_id = self.store.reserve()["artifact_id"]
        second_store = HealthzArtifacts(self.directory, max_artifacts=2,
                                        max_bytes=4096)
        second_staged = threading.Event()
        allow_second = threading.Event()
        file_info = healthz_artifacts._file_info

        def pause_second(path):
            if path == second_source:
                second_staged.set()
                if not allow_second.wait(5):
                    raise TimeoutError("first submission did not finish")
            return file_info(path)

        with patch("host_modules.healthz_artifacts._file_info",
                   side_effect=pause_second):
            with ThreadPoolExecutor(max_workers=1) as pool:
                second = pool.submit(second_store.submit, artifact_id,
                                     [{"path": second_source, "name": "log.txt"}])
                try:
                    self.assertTrue(second_staged.wait(5))
                    first = self.store.submit(artifact_id, [
                        {"path": first_source, "name": "log.txt"}])
                finally:
                    allow_second.set()
                self.assertEqual(second.result(timeout=5), first)

        with tarfile.open(first["location"], "r:gz") as archive:
            self.assertEqual(archive.extractfile("log.txt").read(), b"first")
        self.assertEqual(self.store.status(artifact_id), "COMPLETED")
        self.assertFalse(os.path.exists(self.store._pending(artifact_id)))


if __name__ == "__main__":
    unittest.main()
