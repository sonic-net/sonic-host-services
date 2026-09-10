"""Unit tests for console mirror recording and control components."""

import concurrent.futures
import errno
import json
import os
import queue
import socket
import stat
import struct
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
                msg=f"{invalid} should be invalid",
            ):
                console_mirror.parse_duration(invalid)

    def test_printable_escape_is_utf8_readable_and_terminal_safe(self):
        payload = (
            "SONiC é你好😀".encode("utf-8") + b"\n\r\t\\\x1b[31m\x00\xff\xfe\x80\x9f"
        )
        self.assertEqual(
            console_mirror.printable_escape(payload),
            r"SONiC é你好😀\n\r\t\\\x1b[31m\x00\xff\xfe\x80\x9f",
        )
        self.assertEqual(
            console_mirror.printable_escape("\u200b".encode("utf-8")), r"\xe2\x80\x8b"
        )

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
        self.assertTrue(
            writer.submit_event({"event": "stop", "reason": "manual", "text": "你好"})
        )
        self.assertTrue(writer.close())

        self.assertFalse(writer.submit_data("rx", b"after close"))
        self.assertFalse(writer.submit_event({"event": "after-close"}))
        self.assertEqual(stat.S_IMODE(os.stat(self.tempdir.name).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as recording:
            lines = recording.read().splitlines()
        self.assertEqual(lines[0], "# SONIC_CONSOLE_MIRROR_TEXT version=1")
        self.assertIn("line=1 direction=both", lines[1])
        self.assertRegex(
            lines[3], r"^[^ ]+ \+[0-9]{12}ms 00000001 RX 00000014 Booting SONiC\\n$"
        )
        self.assertIn(r"TX 00000009 \x1b[2Jshow\n", lines[4])
        self.assertTrue(
            lines[5].endswith(
                'EVENT 00000050 {"event":"stop","reason":"manual","text":"你好"}'
            )
        )

    def test_rejects_invalid_line_and_direction(self):
        with self.assertRaises(console_mirror.MirrorError) as caught:
            self.create_writer(line="tty1")
        self.assertEqual(caught.exception.code, "invalid_line")

        with self.assertRaises(console_mirror.MirrorError) as caught:
            self.create_writer(direction="sideways")
        self.assertEqual(caught.exception.code, "invalid_direction")

    def test_root_owned_directories_and_recording_file(self):
        with (
            mock.patch.object(console_mirror.os, "geteuid", return_value=0),
            mock.patch.object(console_mirror.os, "chown") as chown,
            mock.patch.object(console_mirror.os, "fchown") as fchown,
        ):
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
        before_start = console_mirror.WriterRecord(
            "RX", b"\x00", writer.start_timestamp - 1
        )
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
        self.assertFalse(
            writer._submit(mock.sentinel.record, priority=False, nonblocking=True)
        )
        writer._lock.release.assert_not_called()

        writer._lock = threading.Lock()
        writer._accepting = True
        writer.queue_size = 0
        writer._queue = queue.Queue(maxsize=1)
        self.assertFalse(
            writer._submit(mock.sentinel.record, priority=False, nonblocking=False)
        )
        writer._queue.put_nowait(mock.sentinel.queued)
        self.assertFalse(
            writer._submit(mock.sentinel.record, priority=True, nonblocking=False)
        )

    def test_prepare_paths_retries_collision_and_reports_exhaustion(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer.base_dir = self.tempdir.name
        writer.line_dir = os.path.join(self.tempdir.name, "line4")
        writer.line = "4"
        writer.direction = "both"
        writer._open_part = mock.Mock(side_effect=[FileExistsError(), None])
        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory"),
            mock.patch.object(console_mirror.time, "sleep") as sleep,
        ):
            writer._prepare_paths_and_open()
        self.assertEqual(writer._open_part.call_count, 2)
        sleep.assert_called_once_with(0.000001)

        writer._open_part = mock.Mock(side_effect=FileExistsError("collision"))
        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory"),
            mock.patch.object(console_mirror.time, "sleep"),
            self.assertRaises(console_mirror.MirrorError) as caught,
        ):
            writer._prepare_paths_and_open()
        self.assertEqual(caught.exception.code, "file_open_failed")
        self.assertIsInstance(caught.exception.__cause__, FileExistsError)
        self.assertEqual(writer._open_part.call_count, 100)

    def test_open_part_cleans_up_descriptor_when_setup_fails(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer.recording_prefix = os.path.join(self.tempdir.name, "recording")
        with (
            mock.patch.object(console_mirror.os, "open", return_value=42),
            mock.patch.object(
                console_mirror.os, "fchmod", side_effect=OSError("chmod")
            ),
            mock.patch.object(console_mirror.os, "close") as close,
            mock.patch.object(console_mirror.os, "unlink") as unlink,
            self.assertRaisesRegex(OSError, "chmod"),
        ):
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
        with (
            mock.patch.object(console_mirror.os, "open", return_value=42),
            mock.patch.object(console_mirror.os, "fchmod"),
            mock.patch.object(console_mirror.os, "geteuid", return_value=1000),
            mock.patch.object(console_mirror.os, "fdopen", return_value=broken_file),
            mock.patch.object(
                console_mirror.os, "unlink", side_effect=OSError("unlink")
            ),
            self.assertRaisesRegex(OSError, "write"),
        ):
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
        self.assertFalse(writer.close())
        self.assertTrue(writer._closing.is_set())

    def test_close_reports_incomplete_shutdown_when_writer_remains_alive(self):
        writer = object.__new__(console_mirror.RecordingWriter)
        writer._lock = threading.Lock()
        writer._accepting = True
        writer._closing = threading.Event()
        writer.shutdown_timeout = 1
        writer._thread = mock.Mock()
        writer._thread.is_alive.return_value = True

        self.assertFalse(writer.close())

        writer._thread.join.assert_called_once_with(1.5)
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

    def make_job(
        self, part_numbers=(1,), prefix_name="console-mirror-line1-both-session"
    ):
        line_dir = os.path.join(self.tempdir.name, "line1")
        os.makedirs(line_dir, exist_ok=True)
        prefix = os.path.join(line_dir, prefix_name)
        sources = []
        for number in part_numbers:
            source = f"{prefix}-part{number:04d}.log"
            with open(source, "wb") as stream:
                stream.write(f"part{number}".encode("ascii"))
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
            self.assertEqual(
                archive.namelist(), [os.path.basename(path) for path in sources]
            )

    def test_archive_file_is_root_owned_when_running_as_root(self):
        job, _ = self.make_job((1,), "root-owned")
        with (
            mock.patch.object(console_mirror.os, "geteuid", return_value=0),
            mock.patch.object(console_mirror.os, "fchown") as fchown,
        ):
            result = console_mirror.RecordingArchiver._archive(job, threading.Event())
        self.assertEqual(result.archive_path, job.archive_path)
        fchown.assert_called_once()

    def test_missing_and_noncontiguous_parts_are_rejected(self):
        for part_numbers in ((), (2,), (1, 3)):
            job, sources = self.make_job(
                part_numbers, f"session-{len(part_numbers)}"
            )
            self.assert_mirror_error(
                "archive_failed",
                lambda job=job: console_mirror.RecordingArchiver._archive(
                    job, threading.Event()
                ),
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
            job, sources = self.make_job((1,), f"cancel-{index}")
            self.assert_mirror_error(
                "archive_cancelled",
                lambda job=job, sequence=sequence: (
                    console_mirror.RecordingArchiver._archive(
                        job, SequencedEvent(sequence)
                    )
                ),
            )
            self.assertTrue(os.path.exists(sources[0]))
            self.assertFalse(os.path.exists(job.archive_path + ".tmp"))

    def test_zip_validation_rejects_corruption_and_wrong_entry_count(self):
        real_zip_file = console_mirror.zipfile.ZipFile
        validation_results = (("bad-entry", [mock.sentinel.info]), (None, []))
        for index, (bad_entry, infos) in enumerate(validation_results):
            job, sources = self.make_job((1,), f"invalid-zip-{index}")
            invalid_archive = mock.MagicMock()
            invalid_archive.__enter__.return_value.testzip.return_value = bad_entry
            invalid_archive.__enter__.return_value.infolist.return_value = infos

            def zip_file(file, mode="r", *args, **kwargs):
                if mode == "r":
                    return invalid_archive
                return real_zip_file(file, mode, *args, **kwargs)

            with mock.patch.object(
                console_mirror.zipfile, "ZipFile", side_effect=zip_file
            ):
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
        with mock.patch.object(
            console_mirror.os, "open", side_effect=OSError("no space")
        ):
            error = self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(
                    job, threading.Event()
                ),
            )
        self.assertIn("no space", error.message)
        self.assertTrue(os.path.exists(sources[0]))

        job, _ = self.make_job((1,), "fdopen-failure")
        real_close = os.close
        with (
            mock.patch.object(
                console_mirror.os, "fdopen", side_effect=OSError("fdopen")
            ),
            mock.patch.object(console_mirror.os, "close", wraps=real_close) as close,
        ):
            self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(
                    job, threading.Event()
                ),
            )
        close.assert_called_once()
        self.assertFalse(os.path.exists(job.archive_path + ".tmp"))

    def test_temporary_cleanup_failure_is_logged(self):
        job, _ = self.make_job((1,), "cleanup-failure")
        cleanup_error = OSError(errno.EACCES, "permission denied")
        with (
            mock.patch.object(console_mirror.os, "open", side_effect=OSError("create")),
            mock.patch.object(console_mirror.os, "unlink", side_effect=cleanup_error),
            mock.patch.object(console_mirror.log, "warning") as warning,
        ):
            self.assert_mirror_error(
                "archive_failed",
                lambda: console_mirror.RecordingArchiver._archive(
                    job, threading.Event()
                ),
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


class FakeTimer:
    """Deterministic stand-in for threading.Timer used by manager tests."""

    def __init__(self, interval, callback, start_error=None):
        self.interval = interval
        self.callback = callback
        self.start_error = start_error
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class TestMirrorManager(TestCase):
    def setUp(self):
        self.state_table = mock.Mock()
        self.archiver = mock.Mock()
        self.writer = self.make_writer()
        self.writer_factory = mock.Mock(return_value=self.writer)
        self.timers = []
        self.next_timer_error = None

        def make_timer(interval, callback):
            timer = FakeTimer(interval, callback, self.next_timer_error)
            self.next_timer_error = None
            self.timers.append(timer)
            return timer

        self.timer_patch = mock.patch.object(
            console_mirror.threading, "Timer", side_effect=make_timer
        )
        self.timer_patch.start()

    def tearDown(self):
        self.timer_patch.stop()

    @staticmethod
    def make_writer():
        writer = mock.Mock()
        writer.start_timestamp = 1_700_000_000.25
        writer.file_path = "/recordings/line1/session-part0001.log"
        writer.recording_prefix = "/recordings/line1/session"
        writer.submit_event.return_value = True
        writer.submit_data.return_value = True
        writer.close.return_value = True
        return writer

    def make_manager(self, **overrides):
        options = {
            "line": "1",
            "state_table": self.state_table,
            "base_dir": "/recordings",
            "writer_factory": self.writer_factory,
            "archiver": self.archiver,
            "writer_queue_size": 17,
        }
        options.update(overrides)
        return console_mirror.MirrorManager(**options)

    def start_manager(self, manager=None, **options):
        manager = manager or self.make_manager()
        defaults = {
            "direction": "both",
            "timeout": "2h",
            "max_file_size": 8,
            "owner_pid": 1234,
            "started_by": "admin",
        }
        defaults.update(options)
        response = manager.start(defaults)
        return manager, response

    def assert_error(self, code, operation):
        with self.assertRaises(console_mirror.MirrorError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_initialization_writes_idle_state_and_tolerates_state_db_errors(self):
        manager = self.make_manager(line=" 7 ")
        self.assertEqual(manager.line, "7")
        self.state_table.set.assert_called_once_with("7", [("state", "idle")])
        self.assertEqual(
            [call.args for call in self.state_table.hdel.call_args_list],
            [
                ("7", field)
                for field in (
                    "owner_pid",
                    "started_by",
                    "start_time",
                    "timeout",
                    "file_path",
                    "direction",
                )
            ],
        )

        broken_table = mock.Mock()
        broken_table.set.side_effect = RuntimeError("set failed")
        broken_table.hdel.side_effect = RuntimeError("delete failed")
        with mock.patch.object(console_mirror.log, "error") as log_error:
            self.make_manager(line="8", state_table=broken_table)
        self.assertEqual(log_error.call_count, 7)

    def test_start_sets_runtime_timer_writer_and_state_db_metadata(self):
        manager, response = self.start_manager()

        self.writer_factory.assert_called_once_with(
            line="1",
            direction="both",
            timeout_text="2h",
            max_file_size_mb=8,
            base_dir="/recordings",
            queue_size=17,
            on_fatal=manager._on_writer_fatal,
            on_rotate=manager._on_rotate,
        )
        timer = self.timers[-1]
        self.assertEqual(timer.interval, 7200)
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)
        self.writer.submit_event.assert_called_once_with({"event": "start"})
        self.assertEqual(manager.state, "active")
        self.assertIs(manager.writer, self.writer)
        self.assertEqual(manager.direction, "both")
        self.assertEqual(manager.timeout_seconds, 7200)
        self.assertEqual(manager._owner_pid, 1234)
        self.assertEqual(manager._started_by, "admin")
        self.assertEqual(
            response,
            {
                "status": "ok",
                "file_path": self.writer.file_path,
                "timeout": "2h",
                "remaining": "2h",
            },
        )
        self.state_table.set.assert_called_with(
            "1",
            [
                ("state", "active"),
                ("owner_pid", "1234"),
                ("started_by", "admin"),
                ("start_time", "1700000000"),
                ("timeout", "7200"),
                ("file_path", self.writer.file_path),
                ("direction", "both"),
            ],
        )

        with mock.patch.object(manager, "_on_timeout") as on_timeout:
            timer.fire()
        on_timeout.assert_called_once_with(timer)

    def test_start_uses_defaults_and_rejects_invalid_options_or_active_session(self):
        manager = self.make_manager()
        manager.start({"owner_pid": 1234})
        self.writer_factory.assert_called_once_with(
            line="1",
            direction="both",
            timeout_text=console_mirror.DEFAULT_TIMEOUT,
            max_file_size_mb=console_mirror.DEFAULT_MAX_FILE_SIZE_MB,
            base_dir="/recordings",
            queue_size=17,
            on_fatal=manager._on_writer_fatal,
            on_rotate=manager._on_rotate,
        )
        self.assertEqual(manager._owner_pid, 1234)
        self.assertEqual(manager._started_by, "root")
        self.assert_error(
            "mirror_already_active", lambda: manager.start({"owner_pid": 1234})
        )

        for direction in ("sideways", 1):
            fresh = self.make_manager()
            self.assert_error(
                "invalid_direction",
                lambda direction=direction: fresh.start({"direction": direction}),
            )
        for size in (True, "8", 0, -1):
            fresh = self.make_manager()
            self.assert_error(
                "invalid_max_file_size",
                lambda size=size: fresh.start({"max_file_size": size}),
            )

    def test_start_rejects_invalid_owner_pid_before_creating_writer(self):
        for owner_pid in (None, True, False, 0, -1, "1234", "not-a-pid"):
            writer_factory = mock.Mock(return_value=self.make_writer())
            manager = self.make_manager(writer_factory=writer_factory)

            self.assert_error(
                "invalid_owner_pid",
                lambda owner_pid=owner_pid, manager=manager: manager.start(
                    {"owner_pid": owner_pid}
                ),
            )

            writer_factory.assert_not_called()
            self.assertEqual(manager.state, "idle")
            self.assertIsNone(manager.writer)
            self.assertIsNone(manager.timer)

    def test_start_preserves_mirror_errors_and_wraps_writer_open_errors(self):
        expected = console_mirror.MirrorError("unsafe_recording_path", "unsafe")
        manager = self.make_manager(writer_factory=mock.Mock(side_effect=expected))
        self.assertIs(
            self.assert_error(
                "unsafe_recording_path", lambda: manager.start({"owner_pid": 1234})
            ),
            expected,
        )

        manager = self.make_manager(
            writer_factory=mock.Mock(side_effect=OSError("disk error"))
        )
        error = self.assert_error(
            "file_open_failed", lambda: manager.start({"owner_pid": 1234})
        )
        self.assertIn("disk error", error.message)

    def test_start_timer_failure_closes_writer_and_restores_idle_state(self):
        self.next_timer_error = RuntimeError("timer unavailable")
        manager = self.make_manager()
        error = self.assert_error(
            "timer_setup_failed",
            lambda: manager.start({"timeout": "1m", "owner_pid": 1234}),
        )

        self.assertIn("timer unavailable", error.message)
        self.writer.close.assert_called_once_with()
        self.assertEqual(manager.state, "idle")
        self.assertIsNone(manager.writer)
        self.assertIsNone(manager.timer)
        self.assertIsNone(manager.direction)
        self.assertIsNone(manager.start_time)
        self.assertIsNone(manager.timeout_text)
        self.assertIsNone(manager.timeout_seconds)
        self.assertIsNone(manager.deadline)
        self.assertIsNone(manager.file_path)
        self.assertIsNone(manager._owner_pid)
        self.assertIsNone(manager._started_by)
        self.assertEqual(manager.writer_drop_count, 0)
        self.state_table.set.assert_called_with("1", [("state", "idle")])

    def test_submit_is_nonblocking_filters_data_and_tracks_queue_drops(self):
        manager = self.make_manager()
        manager.submit("bad", b"data")
        manager.submit("rx", b"")
        manager.submit("rx", b"idle")
        self.writer.submit_data.assert_not_called()

        busy_lock = mock.Mock()
        busy_lock.acquire.return_value = False
        manager._lock = busy_lock
        manager.submit("rx", b"busy")
        busy_lock.acquire.assert_called_once_with(blocking=False)
        busy_lock.release.assert_not_called()

        manager._lock = threading.RLock()
        manager.state = "active"
        manager.writer = None
        manager.submit("rx", b"no writer")
        manager.writer = self.writer
        manager.direction = "tx"
        manager.submit("rx", b"filtered")
        self.writer.submit_data.assert_not_called()

        manager.direction = "both"
        self.writer.submit_data.return_value = False
        manager.submit("rx", b"first drop")
        self.assertEqual(manager.writer_drop_count, 1)

        self.writer.submit_event.return_value = False
        manager.submit("tx", b"event drop")
        self.assertEqual(manager.writer_drop_count, 2)
        self.writer.submit_data.assert_called_once_with("rx", b"first drop")

        self.writer.submit_event.return_value = True
        self.writer.submit_data.return_value = True
        manager.submit("tx", b"recovered")
        self.writer.submit_event.assert_called_with(
            {"event": "drop", "reason": "writer_queue_full", "count": 2},
            nonblocking=True,
        )
        self.writer.submit_data.assert_called_with("tx", b"recovered")
        self.assertEqual(manager.writer_drop_count, 0)

    def test_update_timeout_rejects_inactive_and_delta_overflow(self):
        manager = self.make_manager()
        self.assert_error("mirror_not_active", lambda: manager.update_timeout("1m"))

        manager.state = "active"
        manager.writer = None
        self.assert_error("mirror_not_active", lambda: manager.update_timeout("1m"))

        manager.writer = self.writer
        manager.start_time = 0
        with mock.patch.object(
            console_mirror.time, "time", return_value=console_mirror.MAX_DELTA_MS / 1000
        ):
            self.assert_error("invalid_timeout", lambda: manager.update_timeout("1s"))

    def test_update_timeout_replaces_timer_and_updates_writer_and_state(self):
        manager, _ = self.start_manager()
        previous = manager.timer
        manager.start_time = 100.0
        with (
            mock.patch.object(console_mirror.time, "time", return_value=99.0),
            mock.patch.object(console_mirror.time, "monotonic", return_value=500.0),
        ):
            response = manager.update_timeout("30m")

        replacement = manager.timer
        self.assertIsNot(replacement, previous)
        self.assertTrue(replacement.started)
        self.assertTrue(previous.cancelled)
        self.assertEqual(manager.timeout_text, "30m")
        self.assertEqual(manager.timeout_seconds, 1800)
        self.assertEqual(manager.deadline, 2300.0)
        self.writer.update_timeout.assert_called_once_with("30m")
        self.writer.submit_event.assert_called_with(
            {"event": "timeout_update", "timeout": "30m"}
        )
        self.assertEqual(
            response, {"status": "ok", "timeout": "30m", "remaining": "30m"}
        )

        with mock.patch.object(manager, "_on_timeout") as on_timeout:
            replacement.fire()
        on_timeout.assert_called_once_with(replacement)

        manager.timer = None
        with mock.patch.object(console_mirror.time, "time", return_value=100.0):
            manager.update_timeout("1m")
        self.assertTrue(manager.timer.started)

    def test_update_timeout_timer_failure_keeps_previous_configuration(self):
        manager, _ = self.start_manager()
        previous = manager.timer
        previous_deadline = manager.deadline
        self.next_timer_error = RuntimeError("cannot start")
        error = self.assert_error(
            "timer_setup_failed", lambda: manager.update_timeout("30m")
        )

        self.assertIn("cannot start", error.message)
        self.assertIs(manager.timer, previous)
        self.assertFalse(previous.cancelled)
        self.assertEqual(manager.timeout_text, "2h")
        self.assertEqual(manager.timeout_seconds, 7200)
        self.assertEqual(manager.deadline, previous_deadline)
        self.writer.update_timeout.assert_not_called()

    def test_stop_without_archive_transitions_through_stopping_and_retains_files(self):
        manager, _ = self.start_manager()
        timer = manager.timer
        states_seen = []

        def capture_state(line, values):
            states_seen.append(dict(values)["state"])

        self.state_table.set.side_effect = capture_state
        response = manager.stop(reason="manual", archive=False)

        self.assertTrue(timer.cancelled)
        self.writer.submit_event.assert_called_with(
            {"event": "stop", "reason": "manual"}
        )
        self.writer.close.assert_called_once_with()
        self.assertEqual(states_seen, ["stopping", "idle"])
        self.assertEqual(manager.state, "idle")
        self.assertIsNone(manager.writer)
        self.assertEqual(
            response,
            {
                "status": "ok",
                "message": "Mirror stopped; recording files retained",
                "recording_prefix": self.writer.recording_prefix,
            },
        )

    def test_stop_validates_state_and_expected_timer(self):
        manager = self.make_manager()
        self.assert_error("mirror_not_active", manager.stop)

        manager.state = "active"
        manager.writer = None
        self.assert_error("mirror_not_active", manager.stop)

        manager, _ = self.start_manager(self.make_manager())
        self.assert_error(
            "stale_timeout",
            lambda: manager.stop(expected_timer=FakeTimer(1, lambda: None)),
        )
        self.assertEqual(manager.state, "active")

        manager.timer = None
        response = manager.stop()
        self.assertEqual(response["status"], "ok")

    def test_stop_does_not_clear_a_writer_replaced_while_old_writer_closes(self):
        manager, _ = self.start_manager()
        replacement_writer = mock.Mock()
        self.writer.close.side_effect = lambda: setattr(
            manager, "writer", replacement_writer
        )

        manager.stop()

        self.assertIs(manager.writer, replacement_writer)
        self.assertEqual(manager.state, "idle")

    def test_stop_archives_an_immutable_session_snapshot(self):
        handle = mock.sentinel.archive_handle
        self.archiver.submit.return_value = handle
        manager, _ = self.start_manager(direction="rx")
        response = manager.stop(
            reason="timeout", archive=True, expected_timer=manager.timer
        )

        job = self.archiver.submit.call_args.args[0]
        self.assertEqual(
            job,
            console_mirror.ArchiveJob(
                line="1",
                direction="rx",
                start_timestamp=self.writer.start_timestamp,
                recording_prefix=self.writer.recording_prefix,
                archive_path=self.writer.recording_prefix + ".zip",
                stop_reason="timeout",
            ),
        )
        self.assertEqual(response["status"], "packaging")
        self.assertEqual(
            response["archive_path"], self.writer.recording_prefix + ".zip"
        )
        self.assertIs(response["archive_handle"], handle)
        self.assertEqual(manager.state, "idle")

    def test_stop_skips_archive_when_writer_shutdown_is_incomplete(self):
        manager, _ = self.start_manager()
        self.writer.close.return_value = False

        error = self.assert_error(
            "writer_shutdown_incomplete", lambda: manager.stop(archive=True)
        )

        self.assertIn("archive was skipped", error.message)
        self.assertIn("source logs were preserved", error.message)
        self.archiver.submit.assert_not_called()
        self.assertEqual(manager.state, "idle")
        self.assertIsNone(manager.writer)

    def test_stop_preserves_archive_errors_and_wraps_submission_failures(self):
        expected = console_mirror.MirrorError("archive_queue_full", "full")
        self.archiver.submit.side_effect = expected
        manager, _ = self.start_manager()
        self.assertIs(
            self.assert_error("archive_queue_full", lambda: manager.stop(archive=True)),
            expected,
        )
        self.assertEqual(manager.state, "idle")

        self.archiver.submit.side_effect = RuntimeError("executor stopped")
        manager, _ = self.start_manager(self.make_manager())
        error = self.assert_error("archive_failed", lambda: manager.stop(archive=True))
        self.assertIn("executor stopped", error.message)
        self.assertIn("source logs were preserved", error.message)

    def test_status_reports_idle_active_stopping_and_clamps_remaining(self):
        manager = self.make_manager()
        self.assertEqual(
            manager.status(), {"status": "ok", "state": "idle", "line": "1"}
        )

        manager, _ = self.start_manager(manager, direction="tx", timeout="1m")
        manager.deadline = 101.2
        with mock.patch.object(console_mirror.time, "monotonic", return_value=100.0):
            active = manager.status()
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["start_time"], "2023-11-14T22:13:20.250Z")
        self.assertEqual(active["direction"], "tx")
        self.assertEqual(active["timeout"], "1m")
        self.assertEqual(active["remaining"], "2s")
        self.assertEqual(active["file_path"], self.writer.file_path)

        manager.state = "stopping"
        manager.deadline = None
        self.assertEqual(manager.status()["remaining"], "0s")

    def test_timeout_callback_ignores_expected_races_and_logs_other_errors(self):
        manager = self.make_manager()
        timer = FakeTimer(1, lambda: None)
        manager.stop = mock.Mock()
        manager._on_timeout(timer)
        manager.stop.assert_called_once_with(
            reason="timeout", archive=True, expected_timer=timer
        )

        for code in ("mirror_not_active", "stale_timeout"):
            manager.stop.side_effect = console_mirror.MirrorError(code, code)
            with mock.patch.object(console_mirror.log, "error") as log_error:
                manager._on_timeout(timer)
            log_error.assert_not_called()

        manager.stop.side_effect = console_mirror.MirrorError(
            "archive_failed", "broken archive"
        )
        with mock.patch.object(console_mirror.log, "error") as log_error:
            manager._on_timeout(timer)
        log_error.assert_called_once_with(
            "[%s] Automatic mirror stop failed: %s", "1", "broken archive"
        )

    def test_writer_fatal_callback_starts_named_daemon_stop_thread(self):
        manager = self.make_manager()
        thread = mock.Mock()
        with (
            mock.patch.object(
                console_mirror.threading, "Thread", return_value=thread
            ) as thread_type,
            mock.patch.object(console_mirror.log, "error") as log_error,
        ):
            manager._on_writer_fatal(self.writer, OSError("disk full"))
        log_error.assert_called_once()
        thread_type.assert_called_once_with(
            target=manager._stop_after_writer_error,
            args=(self.writer,),
            name="console-mirror-writer-error-1",
            daemon=True,
        )
        thread.start.assert_called_once_with()

    def test_stop_after_writer_error_only_stops_matching_active_writer(self):
        manager = self.make_manager()
        manager.stop = mock.Mock()
        manager._stop_after_writer_error(self.writer)
        manager.stop.assert_not_called()

        manager.state = "active"
        manager.writer = mock.Mock()
        manager._stop_after_writer_error(self.writer)
        manager.stop.assert_not_called()

        manager.writer = self.writer
        manager._stop_after_writer_error(self.writer)
        manager.stop.assert_called_once_with(reason="writer_error", archive=False)

        manager.stop.side_effect = console_mirror.MirrorError(
            "mirror_not_active", "raced"
        )
        manager._stop_after_writer_error(self.writer)

    def test_rotate_updates_only_an_active_session(self):
        manager = self.make_manager()
        manager._on_rotate("ignored.log")
        self.assertIsNone(manager.file_path)

        manager, _ = self.start_manager(manager)
        manager._on_rotate("part0002.log")
        self.assertEqual(manager.file_path, "part0002.log")
        self.state_table.set.assert_called_with(
            "1",
            [
                ("state", "active"),
                ("owner_pid", "1234"),
                ("started_by", "admin"),
                ("start_time", "1700000000"),
                ("timeout", "7200"),
                ("file_path", "part0002.log"),
                ("direction", "both"),
            ],
        )

    def test_shutdown_stops_active_session_and_always_shuts_down_archiver(self):
        manager, _ = self.start_manager()
        manager.shutdown(archive_timeout=2.5)
        self.writer.submit_event.assert_called_with(
            {"event": "stop", "reason": "proxy_shutdown"}
        )
        self.archiver.shutdown.assert_called_once_with(timeout=2.5)
        self.assertEqual(manager.state, "idle")

        manager = self.make_manager()
        manager.stop = mock.Mock(
            side_effect=console_mirror.MirrorError("raced", "raced")
        )
        manager.state = "active"
        manager.shutdown(archive_timeout=1)
        manager.stop.assert_called_once_with(reason="proxy_shutdown", archive=False)
        self.archiver.shutdown.assert_called_with(timeout=1)

        manager = self.make_manager()
        manager.shutdown(archive_timeout=0)
        self.archiver.shutdown.assert_called_with(timeout=0)


class TestControlMessageProtocol(TestCase):
    @staticmethod
    def frame(value):
        payload = json.dumps(value).encode("utf-8")
        return struct.pack("!I", len(payload)) + payload

    @staticmethod
    def connection_for(data, chunk_size=None):
        connection = mock.Mock()
        chunks = []
        if chunk_size is None:
            chunks = [data]
        else:
            chunks = [
                data[index : index + chunk_size]
                for index in range(0, len(data), chunk_size)
            ]
        connection.recv.side_effect = chunks
        return connection

    def assert_message_error(self, code, connection):
        with self.assertRaises(console_mirror.MirrorError) as caught:
            console_mirror.recv_message(connection)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_recv_exact_handles_partial_reads_and_clean_eof(self):
        connection = mock.Mock()
        connection.recv.side_effect = [b"ab", b"c", b"d"]
        self.assertEqual(console_mirror._recv_exact(connection, 4), b"abcd")
        self.assertEqual(
            [call.args for call in connection.recv.call_args_list],
            [(4,), (2,), (1,)],
        )

        connection = mock.Mock()
        connection.recv.return_value = b""
        self.assertIsNone(console_mirror._recv_exact(connection, 4))

    def test_recv_message_decodes_framed_json_object(self):
        framed = self.frame({"op": "status", "line": "1", "text": "你好"})
        connection = self.connection_for(framed, chunk_size=2)
        self.assertEqual(
            console_mirror.recv_message(connection),
            {"op": "status", "line": "1", "text": "你好"},
        )

        connection = mock.Mock()
        connection.recv.return_value = b""
        self.assertIsNone(console_mirror.recv_message(connection))

    def test_recv_message_rejects_invalid_lengths_and_truncated_payload(self):
        for length in (0, console_mirror.MAX_CONTROL_MESSAGE + 1):
            connection = mock.Mock()
            connection.recv.return_value = struct.pack("!I", length)
            self.assert_message_error("invalid_message", connection)

        connection = mock.Mock()
        connection.recv.side_effect = [struct.pack("!I", 3), b""]
        error = self.assert_message_error("invalid_message", connection)
        self.assertEqual(error.message, "Truncated control message")

    def test_recv_message_rejects_bad_json_encoding_and_non_object_values(self):
        invalid_payloads = (b"\xff", b"{", b"[]")
        for payload in invalid_payloads:
            connection = mock.Mock()
            connection.recv.side_effect = [struct.pack("!I", len(payload)), payload]
            self.assert_message_error("invalid_message", connection)

    def test_send_message_uses_compact_utf8_length_framing(self):
        connection = mock.Mock()
        console_mirror.send_message(connection, {"status": "ok", "text": "你好"})

        framed = connection.sendall.call_args.args[0]
        length = struct.unpack("!I", framed[:4])[0]
        self.assertEqual(length, len(framed[4:]))
        self.assertEqual(
            json.loads(framed[4:].decode("utf-8")), {"status": "ok", "text": "你好"}
        )
        self.assertNotIn(b" ", framed[4:])


class TestMirrorControlServer(TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        self.server = console_mirror.MirrorControlServer(
            line="1",
            manager=self.manager,
            runtime_dir="/runtime/mirror",
            archive_wait_seconds=3,
            max_clients=2,
        )

    def assert_error_response(self, send, code):
        response = send.call_args.args[1]
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], code)
        return response

    def handle_request(self, request, credentials=(4321, 0, 0)):
        connection = mock.Mock()
        with (
            mock.patch.object(
                self.server, "_peer_credentials", return_value=credentials
            ),
            mock.patch.object(console_mirror, "recv_message", return_value=request),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._handle_client(connection)
        return connection, send

    def test_initialization_validates_line_and_builds_socket_path(self):
        self.assertEqual(self.server.line, "1")
        self.assertEqual(self.server.socket_path, "/runtime/mirror/line1.sock")
        self.assertEqual(self.server.max_clients, 2)
        self.assertTrue(self.server._root_only(0))
        self.assertFalse(self.server._root_only(1000))

        with self.assertRaises(console_mirror.MirrorError) as caught:
            console_mirror.MirrorControlServer("tty1", self.manager)
        self.assertEqual(caught.exception.code, "invalid_line")

    def test_start_replaces_stale_socket_and_starts_daemon_server_thread(self):
        server_socket = mock.Mock()
        worker = mock.Mock()
        socket_info = mock.Mock(st_mode=stat.S_IFSOCK)
        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory") as secure,
            mock.patch.object(console_mirror.os, "lstat", return_value=socket_info),
            mock.patch.object(console_mirror.os, "unlink") as unlink,
            mock.patch.object(
                console_mirror.socket, "socket", return_value=server_socket
            ) as socket_type,
            mock.patch.object(
                console_mirror.threading, "Thread", return_value=worker
            ) as thread_type,
        ):
            self.server.start()

        secure.assert_called_once_with("/runtime/mirror")
        unlink.assert_called_once_with(self.server.socket_path)
        socket_type.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind.assert_called_once_with(self.server.socket_path)
        server_socket.listen.assert_called_once_with(2)
        server_socket.settimeout.assert_called_once_with(0.2)
        self.assertIs(self.server._socket, server_socket)
        self.assertTrue(self.server._running.is_set())
        thread_type.assert_called_once_with(
            target=self.server._serve,
            name="console-mirror-control-1",
            daemon=True,
        )
        worker.start.assert_called_once_with()

    def test_start_accepts_missing_socket_path_and_rejects_occupied_path(self):
        server_socket = mock.Mock()
        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory"),
            mock.patch.object(
                console_mirror.os, "lstat", side_effect=FileNotFoundError
            ),
            mock.patch.object(
                console_mirror.socket, "socket", return_value=server_socket
            ),
            mock.patch.object(
                console_mirror.threading, "Thread", return_value=mock.Mock()
            ),
        ):
            self.server.start()
        self.assertIs(self.server._socket, server_socket)

        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory"),
            mock.patch.object(
                console_mirror.os, "lstat", return_value=mock.Mock(st_mode=stat.S_IFREG)
            ),
            self.assertRaises(console_mirror.MirrorError) as caught,
        ):
            self.server.start()
        self.assertEqual(caught.exception.code, "unsafe_socket_path")

    def test_start_closes_socket_and_cleans_path_when_setup_fails(self):
        server_socket = mock.Mock()
        server_socket.bind.side_effect = OSError("bind failed")
        with (
            mock.patch.object(console_mirror, "_ensure_secure_directory"),
            mock.patch.object(
                console_mirror.os, "lstat", side_effect=FileNotFoundError
            ),
            mock.patch.object(
                console_mirror.os, "unlink", side_effect=OSError("missing")
            ),
            mock.patch.object(
                console_mirror.socket, "socket", return_value=server_socket
            ),
            self.assertRaisesRegex(OSError, "bind failed"),
        ):
            self.server.start()
        server_socket.close.assert_called_once_with()

    def test_serve_handles_timeouts_capacity_limit_clients_and_socket_close(self):
        rejected = mock.Mock()
        accepted = mock.Mock()
        self.server._socket = mock.Mock()
        self.server._socket.accept.side_effect = [
            TimeoutError(),
            (rejected, None),
            (accepted, None),
            OSError("closed"),
        ]
        self.server._running = mock.Mock()
        self.server._running.is_set.side_effect = [True, True, True, True]
        self.server._clients = mock.Mock()
        self.server._clients.acquire.side_effect = [False, True]
        worker = mock.Mock()

        with mock.patch.object(
            console_mirror.threading, "Thread", return_value=worker
        ) as thread_type:
            self.server._serve()

        rejected.close.assert_called_once_with()
        thread_type.assert_called_once_with(
            target=self.server._handle_and_release,
            args=(accepted,),
            name="console-mirror-client-1",
            daemon=True,
        )
        self.assertEqual(self.server._client_threads, [worker])
        worker.start.assert_called_once_with()

        self.server._running.is_set.side_effect = None
        self.server._running.is_set.return_value = False
        self.server._socket.accept.reset_mock()
        self.server._serve()
        self.server._socket.accept.assert_not_called()

    def test_handle_and_release_always_closes_releases_and_removes_worker(self):
        connection = mock.Mock()
        current = threading.current_thread()
        self.server._client_threads = [current]
        self.server._clients = mock.Mock()
        self.server._handle_client = mock.Mock()
        self.server._handle_and_release(connection)
        connection.close.assert_called_once_with()
        self.server._clients.release.assert_called_once_with()
        self.assertEqual(self.server._client_threads, [])

        connection = mock.Mock()
        self.server._client_threads = []
        self.server._clients.reset_mock()
        self.server._handle_client.side_effect = RuntimeError("handler failed")
        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            self.server._handle_and_release(connection)
        connection.close.assert_called_once_with()
        self.server._clients.release.assert_called_once_with()

    def test_peer_credentials_decodes_linux_socket_credentials(self):
        connection = mock.Mock()
        connection.getsockopt.return_value = struct.pack("3i", 123, 456, 789)
        self.assertEqual(self.server._peer_credentials(connection), (123, 456, 789))
        connection.getsockopt.assert_called_once_with(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )

    def test_archive_completion_sends_success_and_cleanup_failure(self):
        connection = mock.Mock()
        handle = mock.Mock()
        handle.result.return_value = console_mirror.ArchiveResult("session.zip")
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        send.assert_called_once_with(
            connection, {"status": "ok", "archive_path": "session.zip"}
        )

        handle.result.return_value = console_mirror.ArchiveResult(
            "session.zip", ("part0001.log", "part0002.log")
        )
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        self.assertEqual(
            send.call_args.args[1],
            {
                "status": "error",
                "code": "archive_cleanup_failed",
                "message": "Archive is valid but some source logs could not be deleted",
                "archive_path": "session.zip",
                "source_paths": ["part0001.log", "part0002.log"],
            },
        )

    def test_archive_completion_cancels_after_server_wait_budget(self):
        connection = mock.Mock()
        handle = mock.Mock()
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 14]),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        handle.cancel.assert_called_once_with()
        handle.result.assert_not_called()
        send.assert_not_called()

    def test_archive_completion_polls_and_returns_when_client_disconnects(self):
        connection = mock.Mock()
        connection.recv.return_value = b""
        handle = mock.Mock()
        handle.result.side_effect = concurrent.futures.TimeoutError
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(
                console_mirror.select, "select", return_value=([connection], [], [])
            ),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        connection.recv.assert_called_once_with(1, socket.MSG_PEEK)
        handle.cancel.assert_not_called()
        send.assert_not_called()

    def test_archive_completion_keeps_waiting_while_client_is_connected(self):
        connection = mock.Mock()
        connection.recv.return_value = b"x"
        handle = mock.Mock()
        result = console_mirror.ArchiveResult("session.zip")
        handle.result.side_effect = [concurrent.futures.TimeoutError, result]
        with (
            mock.patch.object(
                console_mirror.time, "monotonic", side_effect=[10, 10, 10]
            ),
            mock.patch.object(
                console_mirror.select, "select", return_value=([connection], [], [])
            ),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        self.assertEqual(handle.result.call_count, 2)
        send.assert_called_once_with(
            connection, {"status": "ok", "archive_path": "session.zip"}
        )

        handle.reset_mock()
        handle.result.side_effect = [concurrent.futures.TimeoutError, result]
        with (
            mock.patch.object(
                console_mirror.time, "monotonic", side_effect=[10, 10, 10]
            ),
            mock.patch.object(
                console_mirror.select, "select", return_value=([], [], [])
            ),
            mock.patch.object(console_mirror, "send_message"),
        ):
            self.server._send_archive_completion(connection, handle)
        self.assertEqual(handle.result.call_count, 2)

    def test_archive_completion_reports_archive_and_unexpected_failures(self):
        connection = mock.Mock()
        handle = mock.Mock()
        handle.result.side_effect = console_mirror.MirrorError(
            "archive_failed", "zip failed"
        )
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        self.assertEqual(
            send.call_args.args[1],
            {
                "status": "error",
                "code": "archive_failed",
                "message": "zip failed; original log parts were preserved",
            },
        )

        handle.result.side_effect = RuntimeError("worker crashed")
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._send_archive_completion(connection, handle)
        self.assertEqual(send.call_args.args[1]["code"], "archive_failed")
        self.assertIn("worker crashed", send.call_args.args[1]["message"])

    def test_archive_completion_ignores_disconnected_response_socket(self):
        connection = mock.Mock()
        handle = mock.Mock()
        handle.result.return_value = console_mirror.ArchiveResult("session.zip")
        with (
            mock.patch.object(console_mirror.time, "monotonic", side_effect=[10, 10]),
            mock.patch.object(
                console_mirror, "send_message", side_effect=OSError("disconnected")
            ),
        ):
            self.server._send_archive_completion(connection, handle)

    def test_handle_client_dispatches_start_with_authenticated_audit_metadata(self):
        self.manager.start.return_value = {"status": "ok", "file_path": "part.log"}
        request = {
            "op": "start",
            "line": "1",
            "direction": "both",
            "started_by": "admin",
            "owner_pid": 999,
        }
        connection, send = self.handle_request(request)

        options = self.manager.start.call_args.args[0]
        self.assertEqual(options["owner_pid"], 4321)
        self.assertEqual(options["started_by"], "admin")
        send.assert_called_once_with(
            connection, {"status": "ok", "file_path": "part.log"}
        )

    def test_handle_client_falls_back_to_peer_username_or_uid(self):
        self.manager.start.return_value = {"status": "ok"}
        invalid_names = (None, "", "x" * 257)
        for name in invalid_names:
            self.manager.start.reset_mock()
            with mock.patch.object(
                console_mirror.pwd, "getpwuid", return_value=mock.Mock(pw_name="root")
            ):
                self.handle_request({"op": "start", "line": "1", "started_by": name})
            self.assertEqual(self.manager.start.call_args.args[0]["started_by"], "root")

        with mock.patch.object(console_mirror.pwd, "getpwuid", side_effect=KeyError):
            self.handle_request({"op": "start", "line": "1"})
        self.assertEqual(self.manager.start.call_args.args[0]["started_by"], "0")

    def test_handle_client_dispatches_stop_status_and_timeout(self):
        handle = mock.sentinel.archive_handle
        self.manager.stop.return_value = {
            "status": "packaging",
            "archive_path": "session.zip",
            "archive_handle": handle,
        }
        with mock.patch.object(self.server, "_send_archive_completion") as completion:
            connection, send = self.handle_request(
                {"op": "stop", "line": "1", "archive": True}
            )
        self.manager.stop.assert_called_once_with(reason="manual", archive=True)
        send.assert_called_once_with(
            connection, {"status": "packaging", "archive_path": "session.zip"}
        )
        completion.assert_called_once_with(connection, handle)

        self.manager.stop.reset_mock()
        self.manager.stop.return_value = {"status": "ok"}
        with mock.patch.object(self.server, "_send_archive_completion") as completion:
            self.handle_request({"op": "stop", "line": "1"})
        self.manager.stop.assert_called_once_with(reason="manual", archive=False)
        completion.assert_not_called()

        self.manager.status.return_value = {"status": "ok", "state": "idle"}
        connection, send = self.handle_request({"op": "status", "line": "1"})
        send.assert_called_once_with(connection, self.manager.status.return_value)

        self.manager.update_timeout.return_value = {"status": "ok", "timeout": "2h"}
        connection, send = self.handle_request(
            {"op": "timeout", "line": "1", "timeout": "2h"}
        )
        self.manager.update_timeout.assert_called_once_with("2h")
        send.assert_called_once_with(
            connection, self.manager.update_timeout.return_value
        )

    def test_handle_client_rejects_unauthorized_mismatched_and_invalid_requests(self):
        connection, send = self.handle_request(
            {"op": "status", "line": "1"}, credentials=(1, 1000, 1000)
        )
        self.assert_error_response(send, "permission_denied")

        _, send = self.handle_request({"op": "status", "line": "2"})
        self.assert_error_response(send, "line_mismatch")

        _, send = self.handle_request({"op": "stop", "line": "1", "archive": "yes"})
        self.assert_error_response(send, "invalid_archive")

        _, send = self.handle_request({"op": "unknown", "line": "1"})
        self.assert_error_response(send, "unsupported_operation")

        connection = mock.Mock()
        with (
            mock.patch.object(self.server, "_peer_credentials", return_value=(1, 0, 0)),
            mock.patch.object(console_mirror, "recv_message", return_value=None),
            mock.patch.object(console_mirror, "send_message") as send,
        ):
            self.server._handle_client(connection)
        send.assert_not_called()

    def test_handle_client_returns_manager_error_details_and_tolerates_disconnect(self):
        self.manager.status.side_effect = console_mirror.MirrorError(
            "status_failed", "failed", retry_after=3
        )
        connection, send = self.handle_request({"op": "status", "line": "1"})
        self.assertEqual(
            send.call_args.args[1],
            {
                "status": "error",
                "code": "status_failed",
                "message": "failed",
                "retry_after": 3,
            },
        )

        with (
            mock.patch.object(
                self.server, "_peer_credentials", return_value=(1, 1000, 1000)
            ),
            mock.patch.object(
                console_mirror, "send_message", side_effect=OSError("closed")
            ),
        ):
            self.server._handle_client(connection)

    def test_handle_client_reports_unexpected_error_while_sending_error_response(self):
        connection = mock.Mock()
        with (
            mock.patch.object(
                self.server, "_peer_credentials", return_value=(1, 1000, 1000)
            ),
            mock.patch.object(
                console_mirror, "send_message", side_effect=[ValueError("encode"), None]
            ) as send,
            mock.patch.object(console_mirror.log, "exception") as log_exception,
        ):
            self.server._handle_client(connection)
        log_exception.assert_called_once_with(
            "[%s] Unexpected mirror control error", "1"
        )
        self.assertEqual(send.call_args_list[1].args[1]["code"], "internal_error")

        with (
            mock.patch.object(
                self.server, "_peer_credentials", return_value=(1, 1000, 1000)
            ),
            mock.patch.object(
                console_mirror,
                "send_message",
                side_effect=[ValueError("encode"), OSError("closed")],
            ),
        ):
            self.server._handle_client(connection)

    def test_stop_closes_server_joins_workers_and_removes_socket(self):
        server_socket = mock.Mock()
        server_thread = mock.Mock()
        alive_client = mock.Mock()
        alive_client.is_alive.return_value = True
        finished_client = mock.Mock()
        finished_client.is_alive.return_value = False
        self.server._socket = server_socket
        self.server._thread = server_thread
        self.server._running.set()
        self.server._client_threads = [alive_client, finished_client]

        with mock.patch.object(console_mirror.os, "unlink") as unlink:
            self.server.stop()
        self.assertFalse(self.server._running.is_set())
        server_socket.close.assert_called_once_with()
        self.assertIsNone(self.server._socket)
        server_thread.join.assert_called_once_with(1.0)
        self.assertIsNone(self.server._thread)
        alive_client.join.assert_called_once_with(0.1)
        finished_client.join.assert_not_called()
        unlink.assert_called_once_with(self.server.socket_path)

    def test_stop_handles_missing_socket_and_logs_other_cleanup_errors(self):
        missing = OSError(errno.ENOENT, "missing")
        with (
            mock.patch.object(console_mirror.os, "unlink", side_effect=missing),
            mock.patch.object(console_mirror.log, "warning") as warning,
        ):
            self.server.stop()
        warning.assert_not_called()

        denied = OSError(errno.EACCES, "denied")
        with (
            mock.patch.object(console_mirror.os, "unlink", side_effect=denied),
            mock.patch.object(console_mirror.log, "warning") as warning,
        ):
            self.server.stop()
        warning.assert_called_once_with(
            "Failed to remove mirror control socket %s: %s",
            self.server.socket_path,
            denied,
        )
