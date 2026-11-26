"""Generic host Healthz archive lifecycle and path safety."""

import errno
import os
from pathlib import Path
import subprocess
import sys
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

    def test_active_writer_survives_reopen_and_competing_mutations(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("original writer")
        artifact_id = self.store.reserve()["artifact_id"]
        staged = threading.Event()
        resume = threading.Event()
        file_info = healthz_artifacts._file_info

        def pause_writer(path):
            staged.set()
            if not resume.wait(5):
                raise TimeoutError("writer did not resume")
            return file_info(path)

        with patch("host_modules.healthz_artifacts._file_info",
                   side_effect=pause_writer):
            with ThreadPoolExecutor(max_workers=1) as pool:
                writer = pool.submit(self.store.submit, artifact_id,
                                     [{"path": source, "name": "log.txt"}])
                try:
                    self.assertTrue(staged.wait(5))
                    stages = list(Path(self.directory).glob("*.tmp"))
                    self.assertEqual(len(stages), 1)
                    other = HealthzArtifacts(self.directory, max_artifacts=2,
                                             max_bytes=4096)
                    self.assertTrue(stages[0].exists())
                    for call in (other.reserve,
                                 lambda: other.submit(artifact_id, []),
                                 lambda: other.fail(artifact_id)):
                        with self.assertRaises(BlockingIOError) as busy:
                            call()
                        self.assertEqual(busy.exception.errno, errno.EBUSY)
                    self.assertEqual(other.status(artifact_id), "PENDING")
                    self.assertTrue(stages[0].exists())
                finally:
                    resume.set()
                result = writer.result(timeout=5)

        self.assertEqual(other.submit(artifact_id, []), result)
        with tarfile.open(result["location"], "r:gz") as archive:
            self.assertEqual(archive.extractfile("log.txt").read(), b"original writer")
        self.assertEqual(self.store.status(artifact_id), "COMPLETED")
        self.assertFalse(os.path.exists(self.store._pending(artifact_id)))
        self.assertEqual(list(Path(self.directory).glob("*.tmp")), [])

    def test_final_file_appearing_during_submit_is_not_replaced(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("new collection")
        artifact_id = self.store.reserve()["artifact_id"]
        final = self.store._archive(artifact_id)
        file_info = healthz_artifacts._file_info

        def publish_existing(path):
            with open(final, "wb") as output:
                output.write(b"existing archive")
            return file_info(path)

        with patch("host_modules.healthz_artifacts._file_info",
                   side_effect=publish_existing):
            self.store.submit(artifact_id, [{"path": source, "name": "log.txt"}])
        with open(final, "rb") as stream:
            self.assertEqual(stream.read(), b"existing archive")
        self.assertEqual(list(Path(self.directory).glob("*.tmp")), [])

    def test_crash_stages_are_removed_on_reopen_and_same_store_retry(self):
        source = os.path.join(self.root, "log.txt")
        with open(source, "w") as output:
            output.write("tiny crash fixture\n" * 20)
        program = """
import os, sys, tarfile
sys.path.insert(0, sys.argv[1])
from host_modules.healthz_artifacts import HealthzArtifacts
original_addfile = tarfile.TarFile.addfile
def crash_after_member(self, info, fileobj=None):
    result = original_addfile(self, info, fileobj)
    if info.name == 'log.txt':
        self.fileobj.flush()
        os._exit(41)
    return result
tarfile.TarFile.addfile = crash_after_member
store = HealthzArtifacts(sys.argv[2], max_artifacts=2, max_bytes=4096)
store.submit(sys.argv[3], [{'path': sys.argv[4], 'name': 'log.txt'}])
"""
        package_root = str(Path(healthz_artifacts.__file__).resolve().parents[1])
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for reopen in (False, True):
            with self.subTest(reopen=reopen):
                artifact_id = self.store.reserve()["artifact_id"]
                for _ in range(3):
                    child = subprocess.run(
                        [sys.executable, "-B", "-c", program, package_root,
                         self.directory, artifact_id, source],
                        env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 41, child.stderr)
                    self.assertEqual(len(list(Path(self.directory).glob("*.tmp"))), 1)
                    if reopen:
                        self.store = HealthzArtifacts(self.directory, max_artifacts=2,
                                                      max_bytes=4096)
                        self.assertEqual(list(Path(self.directory).glob("*.tmp")), [])
                    self.assertEqual(self.store.status(artifact_id), "PENDING")
                result = self.store.submit(artifact_id, [
                    {"path": source, "name": "log.txt"}])
                self.assertEqual(self.store.status(artifact_id), "COMPLETED")
                self.assertEqual(list(Path(self.directory).glob("*.tmp")), [])
                with tarfile.open(result["location"], "r:gz") as archive:
                    member = archive.extractfile("log.txt")
                    assert member is not None
                    self.assertEqual(member.read(), b"tiny crash fixture\n" * 20)


if __name__ == "__main__":
    unittest.main()
