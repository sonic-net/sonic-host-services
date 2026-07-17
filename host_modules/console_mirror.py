import concurrent.futures
import datetime
import errno
import json
import logging
import math
import os
import pwd
import queue
import re
import select
import socket
import stat
import struct
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("console-monitor.mirror")

DEFAULT_TIMEOUT = "24h"
DEFAULT_MAX_FILE_SIZE_MB = 64
MAX_DELTA_MS = 999999999999  # 12 digits
MAX_PART_NUMBER = 9999
_DURATION_RE = re.compile(r"^([0-9]+)([smhd])$")
MAX_CONTROL_MESSAGE = 64 * 1024
ARCHIVE_WAIT_SECONDS = 10 * 60
MIRROR_BASE_DIR = "/var/log/sonic/console-mirror"
MIRROR_RUNTIME_DIR = "/run/console-monitor/mirror"
VALID_DIRECTIONS = frozenset(("rx", "tx", "both"))


class MirrorError(Exception):
    """An error safe to return over the local control protocol."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

class ArchiveCancelled(Exception):
    pass

def parse_duration(value: Any) -> Tuple[str, int]:
    """Parse a duration string into (orginal text, seconds) tuple. Accepts formats like '30m', '2h', '1d'."""
    if not isinstance(value, str):
        raise MirrorError("invalid_timeout",
                          "Timeout must use the form <integer>[s|m|h|d]")
    match = _DURATION_RE.match(value)
    if not match:
        raise MirrorError("invalid_timeout",
                          "Timeout must use the form <integer>[s|m|h|d]")
    amount, unit = match.groups()
    amount = int(amount)
    if amount <= 0:
        raise MirrorError("invalid_timeout", "Timeout must be positive")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = amount * multipliers[unit]
    if seconds * 1000 > MAX_DELTA_MS:
        raise MirrorError("invalid_timeout",
                          "Timeout exceeds the SCM-Text delta range")
    return value.strip(), seconds


def validate_line(value: Any) -> str:
    """Validate a line number string. Accepts formats like '1', '2', '3'."""
    line = str(value).strip()
    if not re.fullmatch(r"[0-9]+", line):
        raise MirrorError(
            "invalid_line", "Console line must contain decimal digits only")
    return line


def format_remaining(seconds: float) -> str:
    """Format a duration in seconds into a human-readable string like '1d2h3m4s'."""
    remaining = max(0, int(math.ceil(seconds)))
    if remaining == 0:
        return "0s"
    parts = []
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        value, remaining = divmod(remaining, unit)
        if value:
            parts.append("{}{}".format(value, suffix))
    return "".join(parts)


def printable_escape(payload: bytes) -> str:
    """Return a deterministic, terminal-safe representation of a bytes payload."""
    def _escape_control_byte(byte: int) -> Optional[str]:
        """Escape a single control byte"""
        escaped = {
            0x0A: r"\n",
            0x0D: r"\r",
            0x09: r"\t",
            0x5C: r"\\",
            0x1B: r"\x1b",
        }.get(byte)
        if escaped is not None:
            return escaped
        if byte < 0x20 or 0x7F <= byte <= 0x9F:
            return r"\x{:02x}".format(byte)
        return None
    
    def _utf8_width(byte: int) -> int:
        if 0xC2 <= byte <= 0xDF:
            return 2
        if 0xE0 <= byte <= 0xEF:
            return 3
        if 0xF0 <= byte <= 0xF4:
            return 4
        return 1

    def _decode_printable_utf8(payload: bytes, index: int) -> Optional[Tuple[str, int]]:
        width = _utf8_width(payload[index])
        chunk = payload[index:index + width]
        try:
            char = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if len(char) == 1 and char.isprintable():
            return char, width
        return None

    def _escape_byte(payload: bytes, index: int) -> Tuple[str, int]:
        """Escape one unit"""
        byte = payload[index]
        if escaped := _escape_control_byte(byte):
            return escaped, 1
        if byte < 0x80:
            return chr(byte), 1
        if decoded := _decode_printable_utf8(payload, index):
            return decoded
        return r"\x{:02x}".format(byte), 1

    output: List[str] = []
    index = 0
    while index < len(payload):
        escaped, consumed = _escape_byte(payload, index)
        output.append(escaped)
        index += consumed
    return "".join(output)


def _rfc3339_ms(timestamp: float) -> str:
    dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def _timestamp_token(timestamp: float) -> str:
    dt = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S") + "{:06d}Z".format(dt.microsecond)


def _ensure_secure_directory(path: str) -> None:
    """Ensure that the given path exists and is a secure directory (700 permissions)."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MirrorError("unsafe_recording_path",
                          "Recording directory is not a real directory: {}".format(path))
    os.chmod(path, 0o700)
    if os.geteuid() == 0:
        os.chown(path, 0, 0)

@dataclass(frozen=True)
class WriterRecord:
    direction: str
    payload: bytes
    timestamp: float
    is_event: bool = False

class RecordingWriter:
    """Write one mirror session through a bounded background queue.

    Pass the line, direction, timeout text, and per-part MB limit; optional
    arguments configure storage, queue/shutdown limits, and callbacks.
    Construction opens ``part0001`` and starts the worker immediately. Submit
    records with :meth:`submit_data` or :meth:`submit_event`, update future-part
    metadata with :meth:`update_timeout`, and always finish with :meth:`close`.

    ``on_fatal(writer, error)`` reports writer failure; ``on_rotate(path)``
    reports the newly active part. Public methods are thread-safe.
    """

    def __init__(
        self,
        line: str,
        direction: str,
        timeout_text: str,
        max_file_size_mb: int,
        base_dir: str = MIRROR_BASE_DIR,
        queue_size: int = 4096,
        shutdown_timeout: float = 5.0,
        on_fatal: Optional[Callable[["RecordingWriter", Exception], None]] = None,
        on_rotate: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.line = validate_line(line)
        if direction not in VALID_DIRECTIONS:
            raise MirrorError("invalid_direction", "Direction must be rx, tx, or both")
        self.direction = direction
        self.timeout_text = timeout_text
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.base_dir = base_dir
        self.line_dir = os.path.join(base_dir, "line{}".format(line))
        self.queue_size = queue_size
        self.shutdown_timeout = shutdown_timeout
        self.on_fatal = on_fatal
        self.on_rotate = on_rotate

        self.start_timestamp = time.time()
        self.start_monotonic = time.monotonic()
        self.timestamp_token = _timestamp_token(self.start_timestamp)
        self.recording_prefix = ""
        self.file_path = ""
        self.part_number = 1
        self._seq = 0
        self._file = None
        self._file_size = 0
        self._part_has_records = False
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size + 4)
        self._accepting = True
        self._closing = threading.Event()
        self._shutdown_deadline: Optional[float] = None
        self._lock = threading.Lock()
        self._fatal_reported = False

        # Prepare the recording directory and open the first part file
        self._prepare_paths_and_open()
        # Start the background thread to process the queue
        self._thread = threading.Thread(
            target=self._run, name="console-mirror-writer-{}".format(line), daemon=True
        )
        self._thread.start()

    def _prepare_paths_and_open(self) -> None:
        _ensure_secure_directory(self.base_dir)
        _ensure_secure_directory(self.line_dir)
        # Try to create a unique recording file, retrying if it already exists
        last_error = None
        for _ in range(100):
            self.start_timestamp = time.time()
            self.start_monotonic = time.monotonic()
            self.timestamp_token = _timestamp_token(self.start_timestamp)
            basename = "console-mirror-line{}-{}-{}".format(
                self.line, self.direction, self.timestamp_token
            )
            self.recording_prefix = os.path.join(self.line_dir, basename)
            try:
                self._open_part(1, exclusive=True)
                return
            except FileExistsError as error:
                last_error = error
                time.sleep(0.000001)
        raise MirrorError("file_open_failed", "Could not create a unique recording file") from last_error

    def _open_part(self, part_number: int, exclusive: bool = True) -> None:
        path = "{}-part{:04d}.log".format(self.recording_prefix, part_number)
        flags = os.O_WRONLY | os.O_CREAT # Write-only, create if not exists
        if exclusive:
            flags |= os.O_EXCL # Fail if the file already exists
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW # Do not follow symlinks
        fd = os.open(path, flags, 0o600)
        file_object = None
        try:
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            file_object = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd = -1 # Mark fd as closed since we now have a file object
            header = (
                "# SONIC_CONSOLE_MIRROR_TEXT version=1\n"
                "# line={} direction={} start_time={} timeout={} part=part{:04d} encoding=printable-escape\n"
                "# fields=timestamp delta seq direction length payload\n"
            ).format(
                self.line,
                self.direction,
                _rfc3339_ms(self.start_timestamp),
                self.timeout_text,
                part_number,
            )
            file_object.write(header)
            file_object.flush()
            self._file = file_object
            self._file_size = len(header.encode("utf-8"))
            self._part_has_records = False
            self.part_number = part_number
            self.file_path = path
        except Exception:
            if fd >= 0:
                os.close(fd)
            elif file_object is not None:
                try:
                    file_object.close()
                except Exception:
                    pass
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def update_timeout(self, timeout_text: str) -> None:
        """Set the timeout header value used by subsequently rotated parts."""
        with self._lock:
            self.timeout_text = timeout_text

    def submit_data(self, direction: str, payload: bytes) -> bool:
        """Non-blockingly enqueue RX/TX bytes; return whether they were accepted."""
        return self._submit(
            WriterRecord(direction.upper(), bytes(payload), time.time()),
            priority=False,
            nonblocking=True,
        )

    def submit_event(self, event: Dict[str, Any], nonblocking: bool = False) -> bool:
        """Enqueue a JSON event; optionally avoid waiting for the state lock."""
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._submit(
            WriterRecord("EVENT", payload, time.time(), is_event=True),
            priority=True,
            nonblocking=nonblocking,
        )

    def _submit(self, record: WriterRecord, priority: bool, nonblocking: bool) -> bool:
        if not self._lock.acquire(blocking=not nonblocking):
            return False
        try:
            if not self._accepting:
                return False
            # If the queue is full and this is not a priority record, reject it
            if not priority and self._queue.qsize() >= self.queue_size:
                return False
            try:
                self._queue.put_nowait(record)
                return True
            except queue.Full:
                return False
        finally:
            self._lock.release()

    def _render_record(self, record: WriterRecord, seq: int) -> bytes:
        # Calculate the time delta in milliseconds
        delta_ms = int(round((record.timestamp - self.start_timestamp) * 1000))
        delta_ms: int = min(MAX_DELTA_MS, max(0, delta_ms))
        # Decode the payload for display
        payload_text: str = record.payload.decode("utf-8") if record.is_event else printable_escape(record.payload)
        # Format the line
        # timestamp delta seq direction length payload
        # 2026-07-14T12:00:01.000Z +000000001000ms 00000002 RX 00000005 hello
        line: str = "{} +{:012d}ms {:08d} {} {:08d} {}\n".format(
            _rfc3339_ms(record.timestamp),
            delta_ms,
            seq,
            record.direction,
            len(record.payload),
            payload_text,
        )
        return line.encode("utf-8")

    def _rotate(self) -> None:
        next_part = self.part_number + 1
        if next_part > MAX_PART_NUMBER:
            raise MirrorError("part_limit_exceeded", "Recording reached the maximum part count")
        rotate = WriterRecord(
            "EVENT",
            json.dumps(
                {"event": "rotate", "next_part": "part{:04d}".format(next_part)},
                separators=(",", ":"),
            ).encode("utf-8"),
            time.time(),
            is_event=True,
        )
        rotate_bytes = self._render_record(rotate, self._seq + 1)
        # If the current part has enough space, write the rotate event to it before closing
        if self._file_size + len(rotate_bytes) <= self.max_file_size:
            self._file.write(rotate_bytes.decode("utf-8"))
            self._file_size += len(rotate_bytes)
            self._seq += 1
        # Flush and close the current part, then open the next part
        self._file.flush()
        self._file.close()
        self._file = None
        self._open_part(next_part, exclusive=True)
        # Notify the callback that a rotation has occurred
        if self.on_rotate:
            self.on_rotate(self.file_path)

    def _write_record(self, record: WriterRecord) -> None:
        encoded = self._render_record(record, self._seq + 1)
        # Rotate if not the first record and exceeds the max file size
        if self._part_has_records and self._file_size + len(encoded) > self.max_file_size:
            self._rotate()
            # Re-render the record after rotation to get the correct seq
            encoded = self._render_record(record, self._seq + 1)
        self._file.write(encoded.decode("utf-8"))
        self._file_size += len(encoded)
        self._part_has_records = True
        self._seq += 1

    def _run(self) -> None:
        fatal_error = None
        try:
            while True:
                if self._closing.is_set():
                    if self._queue.empty():
                        break
                    if self._shutdown_deadline is not None and time.monotonic() >= self._shutdown_deadline:
                        break
                try:
                    # Wait for a record to be available
                    record = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    self._write_record(record)
                finally:
                    self._queue.task_done()
            self._file.flush()
        except Exception as error:
            fatal_error = error
        finally:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception as error:
                    fatal_error = fatal_error or error
                self._file = None
            if fatal_error is not None:
                self._report_fatal(fatal_error)

    def _report_fatal(self, error: Exception) -> None:
        with self._lock:
            self._accepting = False
            if self._fatal_reported:
                return
            self._fatal_reported = True
        # Call the fatal callback if provided
        if self.on_fatal:
            self.on_fatal(self, error)

    def close(self) -> None:
        """Stop accepting records and wait boundedly for queued writes to finish."""
        with self._lock:
            self._accepting = False
            self._shutdown_deadline = time.monotonic() + self.shutdown_timeout
            self._closing.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(self.shutdown_timeout + 0.5)


@dataclass(frozen=True)
class ArchiveJob:
    line: str
    direction: str
    start_timestamp: float
    recording_prefix: str
    archive_path: str
    stop_reason: str


@dataclass(frozen=True)
class ArchiveResult:
    archive_path: str
    undeleted_sources: Tuple[str, ...] = ()


class ArchiveHandle:
    """Caller-facing handle for waiting on or cancelling an archive job."""

    def __init__(self, future: concurrent.futures.Future, cancel_event: threading.Event) -> None:
        self.future = future
        self.cancel_event = cancel_event

    def result(self, timeout: Optional[float] = None) -> ArchiveResult:
        """Wait for completion and return the result, subject to ``timeout``."""
        return self.future.result(timeout=timeout)

    def cancel(self) -> bool:
        """Request cooperative cancellation; return whether the job was unfinished."""
        self.cancel_event.set()
        return self.future.cancel() or not self.future.done()


class RecordingArchiver:
    """Run immutable archive jobs on one background worker.

    ``max_pending_jobs`` bounds running and queued jobs. Use :meth:`submit` to
    obtain an :class:`ArchiveHandle`, then call :meth:`shutdown` during cleanup.
    """

    def __init__(self, max_pending_jobs: int = 8) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="console-mirror-archiver"
        )
        self._lock = threading.Lock()
        self._handles: List[ArchiveHandle] = []
        self._pending_slots = threading.BoundedSemaphore(max_pending_jobs)

    def submit(self, job: ArchiveJob) -> ArchiveHandle:
        """Queue ``job`` without blocking; raise if capacity is exhausted."""
        if not self._pending_slots.acquire(blocking=False):
            raise MirrorError(
                "archive_queue_full",
                "Archive queue is full; source logs were preserved",
            )
        cancel_event = threading.Event()
        try:
            future = self._executor.submit(self._archive, job, cancel_event)
        except Exception:
            self._pending_slots.release()
            raise
        handle = ArchiveHandle(future, cancel_event)
        with self._lock:
            self._handles.append(handle)
        future.add_done_callback(lambda _: self._discard(handle))
        return handle

    def _discard(self, handle: ArchiveHandle) -> None:
        with self._lock:
            if handle in self._handles:
                self._handles.remove(handle)
        self._pending_slots.release()

    @staticmethod
    def _archive(job: ArchiveJob, cancel_event: threading.Event) -> ArchiveResult:
        # Search for all part files and open
        line_dir = os.path.dirname(job.recording_prefix)
        prefix_name = os.path.basename(job.recording_prefix)
        # Eg: <prefix>-part0001.log
        pattern = re.compile(r"^{}-part([0-9]{{4}})\.log$".format(re.escape(prefix_name)))
        parts: List[Tuple[int, str]] = []
        for entry in os.scandir(line_dir):
            match = pattern.fullmatch(entry.name)
            if match and entry.is_file(follow_symlinks=False):
                parts.append((int(match.group(1)), entry.path))
        parts.sort()
        if not parts or [number for number, _ in parts] != list(range(1, len(parts) + 1)):
            raise MirrorError("archive_failed", "Recording parts are missing or non-contiguous")
        for _, path in parts:
            with open(path, "rb"):
                pass
        
        # Create a temporary archive file and write the parts into it
        temporary_path = job.archive_path + ".tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        try:
             # Create file
            if cancel_event.is_set():
                raise ArchiveCancelled()
            fd = os.open(temporary_path, flags, 0o600)
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)

            # Write parts
            with os.fdopen(fd, "w+b") as archive_file:
                fd = -1
                with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for _, source in parts:
                        if cancel_event.is_set():
                            raise ArchiveCancelled()
                        archive.write(source, arcname=os.path.basename(source))
                        
            # Validate the archive by checking for any bad files and ensuring the number of files matches
            if cancel_event.is_set():
                raise ArchiveCancelled()
            with zipfile.ZipFile(temporary_path, "r") as archive:
                if archive.testzip() is not None or len(archive.infolist()) != len(parts):
                    raise MirrorError("archive_failed", "ZIP validation failed")
            os.replace(temporary_path, job.archive_path)

            # Delete the source part files after successful archiving
            if cancel_event.is_set():
                raise ArchiveCancelled()
            undeleted = []
            for _, source in parts:
                try:
                    os.unlink(source)
                except OSError:
                    undeleted.append(source)

            return ArchiveResult(job.archive_path, tuple(undeleted))
        except ArchiveCancelled:
            raise MirrorError("archive_cancelled", "Archive packaging was cancelled; source logs were preserved")
        except MirrorError:
            raise
        except Exception as error:
            raise MirrorError("archive_failed", "Archive packaging failed: {}".format(error))
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary_path)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    log.warning("Failed to remove temporary archive %s: %s", temporary_path, error)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Wait boundedly, request cancellation, and stop accepting new jobs."""
        with self._lock:
            handles = list(self._handles)
        if handles:
            concurrent.futures.wait([handle.future for handle in handles], timeout=timeout)
        for handle in handles:
            if not handle.future.done():
                handle.cancel()
        self._executor.shutdown(wait=False)


class MirrorManager:
    """Thread-safe per-line mirror state machine."""

    def __init__(
        self,
        line: str,
        state_table: Any,
        base_dir: str = MIRROR_BASE_DIR,
        writer_factory: Callable[..., RecordingWriter] = RecordingWriter,
        archiver: Optional[RecordingArchiver] = None,
        writer_queue_size: int = 4096,
    ) -> None:
        self.line = validate_line(line)
        self.state_table = state_table
        self.base_dir = base_dir
        self.writer_factory = writer_factory
        self.archiver = archiver or RecordingArchiver()
        self.writer_queue_size = writer_queue_size
        self.state = "idle"
        self.direction: Optional[str] = None
        self.start_time: Optional[float] = None
        self.timeout_text: Optional[str] = None
        self.timeout_seconds: Optional[int] = None
        self.deadline: Optional[float] = None
        self.file_path: Optional[str] = None
        self.writer: Optional[RecordingWriter] = None
        self.timer: Optional[threading.Timer] = None
        self.writer_drop_count = 0
        self._owner_pid: Optional[int] = None
        self._started_by: Optional[str] = None
        self._lock = threading.RLock()
        self._write_idle_state()

    def _state_set(self, values: List[Tuple[str, str]]) -> None:
        try:
            self.state_table.set(self.line, values)
        except Exception as error:
            log.error("[%s] Failed to update mirror STATE_DB: %s", self.line, error)

    def _state_delete_fields(self) -> None:
        for field in ("owner_pid", "started_by", "start_time", "timeout", "file_path", "direction"):
            try:
                self.state_table.hdel(self.line, field)
            except Exception as error:
                log.error("[%s] Failed to clear mirror STATE_DB field %s: %s", self.line, field, error)

    def _write_idle_state(self) -> None:
        self._state_set([("state", "idle")])
        self._state_delete_fields()

    def _write_active_state(self) -> None:
        values = [
            ("state", self.state),
            ("owner_pid", str(self._owner_pid)),
            ("started_by", str(self._started_by)),
            ("start_time", str(int(self.start_time))),
            ("timeout", str(self.timeout_seconds)),
            ("file_path", str(self.file_path)),
            ("direction", str(self.direction)),
        ]
        self._state_set(values)

    def start(self, options: Dict[str, Any]) -> Dict[str, Any]:
        direction = options.get("direction", "both")
        if not isinstance(direction, str) or direction not in VALID_DIRECTIONS:
            raise MirrorError("invalid_direction", "Direction must be rx, tx, or both")
        timeout_text, timeout_seconds = parse_duration(options.get("timeout", DEFAULT_TIMEOUT))
        max_file_size = options.get("max_file_size", DEFAULT_MAX_FILE_SIZE_MB)
        if isinstance(max_file_size, bool) or not isinstance(max_file_size, int) or max_file_size <= 0:
            raise MirrorError("invalid_max_file_size", "max_file_size must be a positive integer in MB")

        with self._lock:
            if self.state != "idle":
                raise MirrorError(
                    "mirror_already_active",
                    "Line {} already has an active mirror session".format(self.line),
                )
            try:
                writer = self.writer_factory(
                    line=self.line,
                    direction=direction,
                    timeout_text=timeout_text,
                    max_file_size_mb=max_file_size,
                    base_dir=self.base_dir,
                    queue_size=self.writer_queue_size,
                    on_fatal=self._on_writer_fatal,
                    on_rotate=self._on_rotate,
                )
            except MirrorError:
                raise
            except Exception as error:
                raise MirrorError("file_open_failed", "Failed to open recording file: {}".format(error))

            timer = threading.Timer(
                timeout_seconds, lambda: self._on_timeout(timer)
            )
            timer.daemon = True
            self.writer = writer
            self.direction = direction
            self.start_time = writer.start_timestamp
            self.timeout_text = timeout_text
            self.timeout_seconds = timeout_seconds
            self.deadline = time.monotonic() + timeout_seconds
            self.file_path = writer.file_path
            self._owner_pid = int(options.get("owner_pid", 0))
            self._started_by = str(options.get("started_by", "root"))
            self.writer_drop_count = 0
            self.timer = timer
            self.state = "active"
            try:
                timer.start()
            except Exception as error:
                self.state = "idle"
                self.timer = None
                self.writer = None
                writer.close()
                self._clear_runtime_fields()
                self._write_idle_state()
                raise MirrorError("timer_setup_failed", "Failed to arm mirror timeout: {}".format(error))
            writer.submit_event({"event": "start"})
            self._write_active_state()
            return {
                "status": "ok",
                "file_path": self.file_path,
                "timeout": timeout_text,
                "remaining": timeout_text,
            }

    def _clear_runtime_fields(self) -> None:
        self.direction = None
        self.start_time = None
        self.timeout_text = None
        self.timeout_seconds = None
        self.deadline = None
        self.file_path = None
        self._owner_pid = None
        self._started_by = None
        self.writer_drop_count = 0

    def submit(self, direction: str, payload: bytes) -> None:
        if direction not in ("rx", "tx") or not payload:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self.state != "active" or self.writer is None:
                return
            if self.direction not in (direction, "both"):
                return
            writer = self.writer
            if self.writer_drop_count:
                count = self.writer_drop_count
                if not writer.submit_event(
                    {"event": "drop", "reason": "writer_queue_full", "count": count},
                    nonblocking=True,
                ):
                    self.writer_drop_count += 1
                    return
                self.writer_drop_count = 0
            if not writer.submit_data(direction, payload):
                self.writer_drop_count += 1
        finally:
            self._lock.release()

    def update_timeout(self, timeout_value: Any) -> Dict[str, Any]:
        timeout_text, timeout_seconds = parse_duration(timeout_value)
        replacement = threading.Timer(
            timeout_seconds, lambda: self._on_timeout(replacement)
        )
        replacement.daemon = True
        with self._lock:
            if self.state != "active" or self.writer is None:
                raise MirrorError("mirror_not_active", "Line {} has no active mirror session".format(self.line))
            elapsed_ms = max(0, int((time.time() - self.start_time) * 1000))
            if elapsed_ms + timeout_seconds * 1000 > MAX_DELTA_MS:
                raise MirrorError("invalid_timeout", "Elapsed time plus timeout exceeds the SCM-Text delta range")
            previous_timer = self.timer
            try:
                replacement.start()
            except Exception as error:
                raise MirrorError("timer_setup_failed", "Failed to reset mirror timeout: {}".format(error))
            self.timer = replacement
            self.timeout_text = timeout_text
            self.timeout_seconds = timeout_seconds
            self.deadline = time.monotonic() + timeout_seconds
            self.writer.update_timeout(timeout_text)
            if previous_timer:
                previous_timer.cancel()
            self.writer.submit_event({"event": "timeout_update", "timeout": timeout_text})
            self._write_active_state()
            return {"status": "ok", "timeout": timeout_text, "remaining": timeout_text}

    def stop(
        self,
        reason: str = "manual",
        archive: bool = False,
        expected_timer: Optional[threading.Timer] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.state != "active" or self.writer is None:
                raise MirrorError("mirror_not_active", "Line {} has no active mirror session".format(self.line))
            if expected_timer is not None and self.timer is not expected_timer:
                raise MirrorError("stale_timeout", "Superseded mirror timeout ignored")
            self.state = "stopping"
            timer = self.timer
            self.timer = None
            if timer:
                timer.cancel()
            writer = self.writer
            direction = self.direction
            start_timestamp = self.start_time
            recording_prefix = writer.recording_prefix
            archive_path = recording_prefix + ".zip"
            self._write_active_state()
            writer.submit_event({"event": "stop", "reason": reason})

        writer.close()

        with self._lock:
            if self.writer is writer:
                self.writer = None
            self.state = "idle"
            self._clear_runtime_fields()
            self._write_idle_state()

        if not archive:
            return {
                "status": "ok",
                "message": "Mirror stopped; recording files retained",
                "recording_prefix": recording_prefix,
            }
        job = ArchiveJob(
            line=self.line,
            direction=direction,
            start_timestamp=start_timestamp,
            recording_prefix=recording_prefix,
            archive_path=archive_path,
            stop_reason=reason,
        )
        try:
            handle = self.archiver.submit(job)
        except MirrorError:
            raise
        except Exception as error:
            raise MirrorError(
                "archive_failed",
                "Could not submit archive job: {}; source logs were preserved".format(error),
            )
        return {
            "status": "packaging",
            "message": "Mirror stopped; packaging recording",
            "archive_path": archive_path,
            "archive_handle": handle,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            response: Dict[str, Any] = {"status": "ok", "state": self.state, "line": self.line}
            if self.state in ("active", "stopping"):
                remaining = 0 if self.deadline is None else self.deadline - time.monotonic()
                response.update(
                    {
                        "start_time": _rfc3339_ms(self.start_time),
                        "direction": self.direction,
                        "timeout": self.timeout_text,
                        "remaining": format_remaining(remaining),
                        "file_path": self.file_path,
                    }
                )
            return response

    def _on_timeout(self, fired_timer: threading.Timer) -> None:
        try:
            self.stop(reason="timeout", archive=True, expected_timer=fired_timer)
        except MirrorError as error:
            if error.code not in ("mirror_not_active", "stale_timeout"):
                log.error("[%s] Automatic mirror stop failed: %s", self.line, error.message)

    def _on_writer_fatal(self, writer: RecordingWriter, error: Exception) -> None:
        log.error("[%s] Fatal mirror writer error: %s", self.line, error)
        threading.Thread(
            target=self._stop_after_writer_error,
            args=(writer,),
            name="console-mirror-writer-error-{}".format(self.line),
            daemon=True,
        ).start()

    def _stop_after_writer_error(self, failed_writer: RecordingWriter) -> None:
        with self._lock:
            if self.state != "active" or self.writer is not failed_writer:
                return
        try:
            self.stop(reason="writer_error", archive=False)
        except MirrorError:
            pass

    def _on_rotate(self, file_path: str) -> None:
        with self._lock:
            if self.state != "active":
                return
            self.file_path = file_path
            self._write_active_state()

    def shutdown(self, archive_timeout: float = 5.0) -> None:
        with self._lock:
            active = self.state == "active"
        if active:
            try:
                self.stop(reason="proxy_shutdown", archive=False)
            except MirrorError:
                pass
        self.archiver.shutdown(timeout=archive_timeout)
        with self._lock:
            self.state = "idle"
            self._write_idle_state()
