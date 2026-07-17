"""Unit tests for the console mirror recording writer and archiver."""

import concurrent.futures
import errno
import os
import queue
import stat
import tempfile
import threading
import time
import zipfile
from unittest import TestCase, mock

from host_modules import console_mirror


class TestFormatting(TestCase):
    def test_duration_validation_and_remaining_format(self):
        self.assertEqual(console_mirror.parse_duration("2h"), ("2h", 7200))
        self.assertEqual(console_mirror.format_remaining(44100), "12h15m")
        self.assertEqual(console_mirror.format_remaining(-0.1), "0s")
        for invalid in ("0s", "-1h", "1.5h", "forever", "1h30m", 123, "10x", "999999d"):
            with self.assertRaises(
                console_mirror.MirrorError,
                msg="{} should be invalid".format(invalid),
            ):
                console_mirror.parse_duration(invalid)

    def test_printable_escape_is_utf8_readable_and_terminal_safe(self):
        payload = "SONiC é你好😀".encode("utf-8") + b"\n\r\t\\\x1b[31m\x00\xff\xfe\x80\x9f"
        self.assertEqual(
            console_mirror.printable_escape(payload),
            r"SONiC é你好😀\n\r\t\\\x1b[31m\x00\xff\xfe\x80\x9f",
        )
        self.assertEqual(console_mirror.printable_escape("\u200b".encode("utf-8")), r"\xe2\x80\x8b")

    def test_secure_directory_rejects_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tempdir:
            target = os.path.join(tempdir, "target")
            link = os.path.join(tempdir, "link")
            os.mkdir(target)
            os.symlink(target, link)
            with self.assertRaises(console_mirror.MirrorError) as caught:
                console_mirror._ensure_secure_directory(link)
        self.assertEqual(caught.exception.code, "unsafe_recording_path")


class TestRecordingWriter(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.writers = []

    def tearDown(self):
        for writer in self.writers:
            writer.close()
        self.tempdir.cleanup()

    def create_writer(self, **overrides):
        options = {
            "line": "1",
            "direction": "both",
            "timeout_text": "24h",
            "max_file_size_mb": 64,
            "base_dir": self.tempdir.name,
            "queue_size": 8,
            "shutdown_timeout": 1,
        }
        options.update(overrides)
        writer = console_mirror.RecordingWriter(**options)
        self.writers.append(writer)
        return writer

    def test_headers_records_permissions_and_close(self):
        writer = self.create_writer()
        path = writer.file_path
        self.assertTrue(writer.submit_data("rx", b"Booting SONiC\n"))
        self.assertTrue(writer.submit_data("tx", bytearray(b"\x1b[2Jshow\n")))
        self.assertTrue(writer.submit_event({"event": "stop", "reason": "manual", "text": "你好"}))
        writer.close()

        self.assertFalse(writer.submit_data("rx", b"after close"))
        self.assertFalse(writer.submit_event({"event": "after-close"}))
        self.assertEqual(stat.S_IMODE(os.stat(self.tempdir.name).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as recording:
            lines = recording.read().splitlines()
        self.assertEqual(lines[0], "# SONIC_CONSOLE_MIRROR_TEXT version=1")
        self.assertIn("line=1 direction=both", lines[1])
        self.assertRegex(lines[3], r"^[^ ]+ \+[0-9]{12}ms 00000001 RX 00000014 Booting SONiC\\n$")
        self.assertIn(r"TX 00000009 \x1b[2Jshow\n", lines[4])
        self.assertTrue(lines[5].endswith(
            'EVENT 00000050 {"event":"stop","reason":"manual","text":"你好"}'
        ))

    def test_rejects_invalid_line_and_direction(self):
        with self.assertRaises(console_mirror.MirrorError) as caught:
            self.create_writer(line="tty1")
        self.assertEqual(caught.exception.code, "invalid_line")

        with self.assertRaises(console_mirror.MirrorError) as caught:
            self.create_writer(direction="sideways")
        self.assertEqual(caught.exception.code, "invalid_direction")

    def test_root_owned_directories_and_recording_file(self):
        with mock.patch.object(console_mirror.os, "geteuid", return_value=0), \
                mock.patch.object(console_mirror.os, "chown") as chown, \
                mock.patch.object(console_mirror.os, "fchown") as fchown:
            writer = self.create_writer(line="5", direction="tx")
            writer.close()
        self.assertEqual(chown.call_count, 2)
        fchown.assert_called_once()

    def test_rotation_updates_header_and_calls_callback(self):
        on_rotate = mock.Mock()
        writer = self.create_writer(line="2", direction="rx", on_rotate=on_rotate)
        writer.max_file_size = 700
        self.assertTrue(writer.submit_data("rx", b"a" * 200))
        writer.update_timeout("30m")
        self.assertTrue(writer.submit_data("rx", b"b" * 300))
        writer.close()

        parts = sorted(os.listdir(os.path.join(self.tempdir.name, "line2")))
        self.assertEqual(len(parts), 2)
        first_path = os.path.join(self.tempdir.name, "line2", parts[0])
        second_path = os.path.join(self.tempdir.name, "line2", parts[1])
        with open(first_path, encoding="utf-8") as first:
            self.assertIn('{"event":"rotate","next_part":"part0002"}', first.read())
        with open(second_path, encoding="utf-8") as second:
            second_text = second.read()
        self.assertIn("timeout=30m part=part0002", second_text)
        self.assertIn("00000003 RX", second_text)
        on_rotate.assert_called_once_with(second_path)

    def test_rotation_omits_event_when_current_part_has_no_space(self):
        writer = self.create_writer(line="3", direction="rx")
        self.assertTrue(writer.submit_data("rx", b"first"))
        writer._queue.join()
        writer.max_file_size = writer._file_size
        self.assertTrue(writer.submit_data("rx", b"second"))
        writer.close()

        part_paths = sorted(
            os.path.join(writer.line_dir, name) for name in os.listdir(writer.line_dir)
        )
        self.assertEqual(len(part_paths), 2)
        with open(part_paths[0], encoding="utf-8") as first:
            self.assertNotIn('"event":"rotate"', first.read())
        with open(part_paths[1], encoding="utf-8") as second:
            self.assertIn("00000002 RX", second.read())

    def test_rotation_rejects_excessive_part_number(self):
        writer = self.create_writer(line="6", direction="tx")
        writer.part_number = console_mirror.MAX_PART_NUMBER
        with self.assertRaises(console_mirror.MirrorError) as caught:
            writer._rotate()
        self.assertEqual(caught.exception.code, "part_limit_exceeded")

    def test_render_record_clamps_delta_and_handles_event_payload(self):
        writer = self.create_writer()
        before_start = console_mirror.WriterRecord("RX", b"\x00", writer.start_timestamp - 1)
        far_future = console_mirror.WriterRecord(
            "EVENT",
            b'{"event":"future"}',
            writer.start_timestamp + (console_mirror.MAX_DELTA_MS / 1000) + 10,
            is_event=True,
        )
        self.assertIn(b"+000000000000ms", writer._render_record(before_start, 7))
        rendered = writer._render_record(far_future, 8)
        self.assertIn(b"+999999999999ms", rendered)
        self.assertIn(b'00000008 EVENT 00000018 {"event":"future"}', rendered)

    def test_submit_rejects_busy_lock_queue_limit_and_full_priority_queue(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer._lock = mock.Mock()
        writer._lock.acquire.return_value = False
        self.assertFalse(writer._submit(mock.sentinel.record, priority=False, nonblocking=True))
        writer._lock.release.assert_not_called()

        writer._lock = threading.Lock()
        writer._accepting = True
        writer.queue_size = 0
        writer._queue = queue.Queue(maxsize=1)
        self.assertFalse(writer._submit(mock.sentinel.record, priority=False, nonblocking=False))
        writer._queue.put_nowait(mock.sentinel.queued)
        self.assertFalse(writer._submit(mock.sentinel.record, priority=True, nonblocking=False))

    def test_prepare_paths_retries_collision_and_reports_exhaustion(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer.base_dir = self.tempdir.name
        writer.line_dir = os.path.join(self.tempdir.name, "line4")
        writer.line = "4"
        writer.direction = "both"
        writer._open_part = mock.Mock(side_effect=[FileExistsError(), None])
        with mock.patch.object(console_mirror, "_ensure_secure_directory"), \
                mock.patch.object(console_mirror.time, "sleep") as sleep:
            writer._prepare_paths_and_open()
        self.assertEqual(writer._open_part.call_count, 2)
        sleep.assert_called_once_with(0.000001)

        writer._open_part = mock.Mock(side_effect=FileExistsError("collision"))
        with mock.patch.object(console_mirror, "_ensure_secure_directory"), \
                mock.patch.object(console_mirror.time, "sleep"), \
                self.assertRaises(console_mirror.MirrorError) as caught:
            writer._prepare_paths_and_open()
        self.assertEqual(caught.exception.code, "file_open_failed")
        self.assertIsInstance(caught.exception.__cause__, FileExistsError)
        self.assertEqual(writer._open_part.call_count, 100)

    def test_open_part_cleans_up_descriptor_when_setup_fails(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer.recording_prefix = os.path.join(self.tempdir.name, "recording")
        with mock.patch.object(console_mirror.os, "open", return_value=42), \
                mock.patch.object(console_mirror.os, "fchmod", side_effect=OSError("chmod")), \
                mock.patch.object(console_mirror.os, "close") as close, \
                mock.patch.object(console_mirror.os, "unlink") as unlink, \
                self.assertRaisesRegex(OSError, "chmod"):
            writer._open_part(1)
        close.assert_called_once_with(42)
        unlink.assert_called_once_with(writer.recording_prefix + "-part0001.log")

    def test_open_part_cleans_up_file_object_even_if_close_and_unlink_fail(self):
        broken_file = mock.Mock()
        broken_file.write.side_effect = OSError("write")
        broken_file.close.side_effect = OSError("close")
        writer = object.__new__(console_mirror.RecordingWriter)
        writer.recording_prefix = os.path.join(self.tempdir.name, "recording")
        writer.line = "1"
        writer.direction = "rx"
        writer.start_timestamp = 0
        writer.timeout_text = "1h"
        with mock.patch.object(console_mirror.os, "open", return_value=42), \
                mock.patch.object(console_mirror.os, "fchmod"), \
                mock.patch.object(console_mirror.os, "geteuid", return_value=1000), \
                mock.patch.object(console_mirror.os, "fdopen", return_value=broken_file), \
                mock.patch.object(console_mirror.os, "unlink", side_effect=OSError("unlink")), \
                self.assertRaisesRegex(OSError, "write"):
            writer._open_part(1, exclusive=False)
        broken_file.close.assert_called_once_with()

    def test_run_handles_empty_poll_shutdown_deadline_and_write_failure(self):
        class EmptyOnceQueue:
            def __init__(self, closing):
                self.closing = closing

            def empty(self):
                return True

            def get(self, timeout):
                self.closing.set()
                raise queue.Empty()

        writer = object.__new__(console_mirror.RecordingWriter)
        writer._closing = threading.Event()
        writer._queue = EmptyOnceQueue(writer._closing)
        writer._file = mock.Mock()
        recording = writer._file
        writer._run()
        recording.flush.assert_called_once_with()
        recording.close.assert_called_once_with()
        self.assertIsNone(writer._file)

        writer = object.__new__(console_mirror.RecordingWriter)
        writer._closing = threading.Event()
        writer._closing.set()
        writer._queue = mock.Mock()
        writer._queue.empty.return_value = False
        writer._shutdown_deadline = 0
        writer._file = mock.Mock()
        writer._run()
        writer._queue.get.assert_not_called()

        writer = object.__new__(console_mirror.RecordingWriter)
        writer._closing = threading.Event()
        writer._closing.set()
        writer._shutdown_deadline = None
        writer._queue = mock.Mock()
        writer._queue.empty.side_effect = [False]
        writer._queue.get.return_value = mock.sentinel.record
        writer._write_record = mock.Mock(side_effect=OSError("disk full"))
        writer._file = mock.Mock()
        writer._file.close.side_effect = OSError("close too")
        writer._lock = threading.Lock()
        writer._accepting = True
        writer._fatal_reported = False
        writer.on_fatal = mock.Mock()
        writer._run()
        writer._queue.task_done.assert_called_once_with()
        writer.on_fatal.assert_called_once()
        self.assertEqual(str(writer.on_fatal.call_args.args[1]), "disk full")

    def test_report_fatal_is_idempotent_and_close_from_worker_does_not_join(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer._lock = threading.Lock()
        writer._accepting = True
        writer._fatal_reported = False
        writer.on_fatal = None
        writer._report_fatal(OSError("first"))
        writer._report_fatal(OSError("second"))
        self.assertFalse(writer._accepting)

        writer.shutdown_timeout = 1
        writer._closing = threading.Event()
        writer._thread = threading.current_thread()
        writer.close()
        self.assertTrue(writer._closing.is_set())


class TestRecordingArchiver(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.archivers = []

    def tearDown(self):
        for archiver in self.archivers:
            archiver.shutdown(timeout=1)
        self.tempdir.cleanup()

    def create_archiver(self, **options):
        archiver = console_mirror.RecordingArchiver(**options)
        self.archivers.append(archiver)
        return archiver

    def make_job(self, part_numbers=(1,), prefix_name="console-mirror-line1-both-session"):
        line_dir = os.path.join(self.tempdir.name, "line1")
        os.makedirs(line_dir, exist_ok=True)
        prefix = os.path.join(line_dir, prefix_name)
        sources = []
        for number in part_numbers:
            source = "{}-part{:04d}.log".format(prefix, number)
            with open(source, "wb") as stream:
                stream.write("part{}".format(number).encode("ascii"))
            sources.append(source)
        job = console_mirror.ArchiveJob(
            "1", "both", time.time(), prefix, prefix + ".zip", "manual"
        )
        return job, sources

    def assert_mirror_error(self, code, operation):
        with self.assertRaises(console_mirror.MirrorError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_archive_is_valid_and_sources_are_removed_after_success(self):
        job, sources = self.make_job((1, 2))
        os.mkdir(job.recording_prefix + "-part0003.log")
        with open(job.recording_prefix + "-notes.txt", "w") as stream:
            stream.write("ignore me")

        archiver = self.create_archiver()
        result = archiver.submit(job).result(timeout=3)

        self.assertEqual(result, console_mirror.ArchiveResult(job.archive_path))
        self.assertTrue(all(not os.path.exists(source) for source in sources))
        self.assertEqual(stat.S_IMODE(os.stat(result.archive_path).st_mode), 0o600)
        with zipfile.ZipFile(result.archive_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), [os.path.basename(path) for path in sources])

    def test_archive_file_is_root_owned_when_running_as_root(self):
        job, _ = self.make_job((1,), "root-owned")
        with mock.patch.object(console_mirror.os, "geteuid", return_value=0), \
                mock.patch.object(console_mirror.os, "fchown") as fchown:
            result = console_mirror.RecordingArchiver._archive(job, threading.Event())
        self.assertEqual(result.archive_path, job.archive_path)
        fchown.assert_called_once()

    def test_missing_and_noncontiguous_parts_are_rejected(self):
        for part_numbers in ((), (2,), (1, 3)):
            job, sources = self.make_job(part_numbers, "session-{}".format(len(part_numbers)))
            self.assert_mirror_error(
                "archive_failed",
                lambda job=job: console_mirror.RecordingArchiver._archive(job, threading.Event()),
            )
            self.assertTrue(all(os.path.exists(source) for source in sources))
            self.assertFalse(os.path.exists(job.archive_path))

    def test_archive_queue_is_bounded_and_slot_is_released(self):
        gate = threading.Event()
        archiver = self.create_archiver(max_pending_jobs=1)
        archiver._archive = lambda job, cancel: (
            gate.wait(2) and console_mirror.ArchiveResult(job.archive_path)
        )
        job, _ = self.make_job()
        first = archiver.submit(job)
        self.assert_mirror_error("archive_queue_full", lambda: archiver.submit(job))
        gate.set()
        self.assertEqual(first.result(timeout=2).archive_path, job.archive_path)

        deadline = time.time() + 2
        while archiver._handles and time.time() < deadline:
            time.sleep(0.001)
        second = archiver.submit(job)
        self.assertEqual(second.result(timeout=2).archive_path, job.archive_path)

    def test_submit_releases_slot_when_executor_rejects_job(self):
        archiver = self.create_archiver(max_pending_jobs=1)
        archiver._executor.submit = mock.Mock(side_effect=RuntimeError("shut down"))
        job, _ = self.make_job()
        with self.assertRaisesRegex(RuntimeError, "shut down"):
            archiver.submit(job)
        self.assertTrue(archiver._pending_slots.acquire(blocking=False))
        archiver._pending_slots.release()

    def test_discard_tolerates_handle_already_removed(self):
        archiver = self.create_archiver(max_pending_jobs=1)
        self.assertTrue(archiver._pending_slots.acquire(blocking=False))
        future = concurrent.futures.Future()
        future.set_result(console_mirror.ArchiveResult("archive.zip"))
        handle = console_mirror.ArchiveHandle(future, threading.Event())
        archiver._discard(handle)

    def test_archive_handle_result_and_cancel_outcomes(self):
        future = concurrent.futures.Future()
        expected = console_mirror.ArchiveResult("archive.zip")
        future.set_result(expected)
        handle = console_mirror.ArchiveHandle(future, threading.Event())
        self.assertEqual(handle.result(timeout=0), expected)
        self.assertFalse(handle.cancel())
        self.assertTrue(handle.cancel_event.is_set())

        pending = concurrent.futures.Future()
        pending_handle = console_mirror.ArchiveHandle(pending, threading.Event())
        self.assertTrue(pending_handle.cancel())

    def test_cancelled_at_each_archive_checkpoint_preserves_sources(self):
        class SequencedEvent:
            def __init__(self, values):
                self.values = iter(values)

            def is_set(self):
                return next(self.values)

        checkpoints = (
            [True],
            [False, True],
            [False, False, True],
            [False, False, False, True],
        )
        for index, sequence in enumerate(checkpoints):
            job, sources = self.make_job((1,), "cancel-{}".format(index))
            self.assert_mirror_error(
                "archive_cancelled",
                lambda job=job, sequence=sequence: console_mirror.RecordingArchiver._archive(
                    job, SequencedEvent(sequence)
                ),
            )
            self.assertTrue(os.path.exists(sources[0]))
            self.assertFalse(os.path.exists(job.archive_path + ".tmp"))

    def test_zip_validation_rejects_corruption_and_wrong_entry_count(self):
        real_zip_file = console_mirror.zipfile.ZipFile
        validation_results = (("bad-entry", [mock.sentinel.info]), (None, []))
        for index, (bad_entry, infos) in enumerate(validation_results):
            job, sources = self.make_job((1,), "invalid-zip-{}".format(index))
            invalid_archive = mock.MagicMock()
            invalid_archive.__enter__.return_value.testzip.return_value = bad_entry
            invalid_archive.__enter__.return_value.infolist.return_value = infos

            def zip_file(file, mode="r", *args, **kwargs):
                if mode == "r":
                    return invalid_archive
                return real_zip_file(file, mode, *args, **kwargs)

            with mock.patch.object(console_mirror.zipfile, "ZipFile", side_effect=zip_file):
                self.assert_mirror_error(
                    "archive_failed",
                    lambda job=job: console_mirror.RecordingArchiver._archive(
                        job, threading.Event()
                    ),
                )
            self.assertTrue(os.path.exists(sources[0]))
            self.assertFalse(os.path.exists(job.archive_path))
            self.assertFalse(os.path.exists(job.archive_path + ".tmp"))

    def test_archive_reports_undeleted_sources(self):
        job, sources = self.make_job((1, 2))
        real_unlink = os.unlink

        def unlink(path):
            if path == sources[0]:
                raise OSError(errno.EACCES, "permission denied")
            return real_unlink(path)

        with mock.patch.object(console_mirror.os, "unlink", side_effect=unlink):
            result = console_mirror.RecordingArchiver._archive(job, threading.Event())
        self.assertEqual(result.undeleted_sources, (sources[0],))
        self.assertTrue(os.path.exists(sources[0]))
        self.assertFalse(os.path.exists(sources[1]))

    def test_archive_wraps_creation_errors_and_closes_unwrapped_descriptor(self):
        job, sources = self.make_job((1,), "open-failure")
        with mock.patch.object(console_mirror.os, "open", side_effect=OSError("no space")):
            error = self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(job, threading.Event()),
            )
        self.assertIn("no space", error.message)
        self.assertTrue(os.path.exists(sources[0]))

        job, _ = self.make_job((1,), "fdopen-failure")
        real_close = os.close
        with mock.patch.object(console_mirror.os, "fdopen", side_effect=OSError("fdopen")), \
                mock.patch.object(console_mirror.os, "close", wraps=real_close) as close:
            self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(job, threading.Event()),
            )
        close.assert_called_once()
        self.assertFalse(os.path.exists(job.archive_path + ".tmp"))

    def test_temporary_cleanup_failure_is_logged(self):
        job, _ = self.make_job((1,), "cleanup-failure")
        cleanup_error = OSError(errno.EACCES, "permission denied")
        with mock.patch.object(console_mirror.os, "open", side_effect=OSError("create")), \
                mock.patch.object(console_mirror.os, "unlink", side_effect=cleanup_error), \
                mock.patch.object(console_mirror.log, "warning") as warning:
            self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(job, threading.Event()),
            )
        warning.assert_called_once_with(
            "Failed to remove temporary archive %s: %s",
            job.archive_path + ".tmp",
            cleanup_error,
        )

    def test_shutdown_cancels_unfinished_handles(self):
        archiver = object.__new__(console_mirror.RecordingArchiver)
        archiver._lock = threading.Lock()
        future = concurrent.futures.Future()
        handle = console_mirror.ArchiveHandle(future, threading.Event())
        archiver._handles = [handle]
        archiver._executor = mock.Mock()
        archiver.shutdown(timeout=0)
        self.assertTrue(handle.cancel_event.is_set())
        self.assertTrue(future.cancelled())
        archiver._executor.shutdown.assert_called_once_with(wait=False)
