"""Shell-free subprocess and i2c-tools command construction helpers."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Type

DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


def _bounded_bytes(value, limit: int) -> bytes:
    if value is None:
        return b""
    data = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    return data[:limit]


@dataclass(frozen=True)
class ShellFreeResult:
    argv: Tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def stdout_text(self, encoding="utf-8", errors="replace") -> str:
        return self.stdout.decode(encoding, errors)

    def stderr_text(self, encoding="utf-8", errors="replace") -> str:
        return self.stderr.decode(encoding, errors)


def _run_bounded_process(
    command: Sequence[str], timeout: Optional[float], limit: int
) -> subprocess.CompletedProcess[bytes]:
    """Drain both pipes while retaining at most one overflow byte per stream."""

    output = [bytearray(), bytearray()]
    with subprocess.Popen(
        command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for index, stream in enumerate((process.stdout, process.stderr)):
                    selector.register(stream, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = (
                        None if deadline is None
                        else max(0, deadline - time.monotonic())
                    )
                    if remaining == 0 and timeout is not None:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _events in selector.select(remaining):
                        data = os.read(key.fd, 64 * 1024)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = output[key.data]
                        buffer.extend(data[:max(0, limit + 1 - len(buffer))])
            remaining = (
                None if deadline is None else max(0, deadline - time.monotonic())
            )
            returncode = process.wait(timeout=remaining)
        except BaseException as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                error.output = bytes(output[0][:limit])
                error.stderr = bytes(output[1][:limit])
            raise
    return subprocess.CompletedProcess(
        command, returncode, bytes(output[0]), bytes(output[1])
    )


def run_shell_free(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    runner: Optional[Callable[..., Any]] = None,
) -> ShellFreeResult:
    """Run a bounded argv command without invoking a shell."""

    if isinstance(argv, (str, bytes)):
        raise ValueError("command argv must be a non-empty string sequence")
    command = tuple(argv)
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("command argv must be a non-empty string sequence")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer")
    if runner is None:
        completed = _run_bounded_process(list(command), timeout, max_output_bytes)
    else:
        completed = runner(
            list(command), shell=False, check=False, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
        )
    stdout = _bounded_bytes(completed.stdout, max_output_bytes + 1)
    stderr = _bounded_bytes(completed.stderr, max_output_bytes + 1)
    return ShellFreeResult(
        command,
        int(completed.returncode),
        stdout[:max_output_bytes],
        stderr[:max_output_bytes],
        len(stdout) > max_output_bytes,
        len(stderr) > max_output_bytes,
    )


def run_checked_shell_free(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    runner: Optional[Callable[..., Any]] = None,
    error_type: Type[Exception] = RuntimeError,
    error_context: Optional[str] = None,
    encoding: str = "utf-8",
    encoding_errors: str = "replace",
    error_encoding: Optional[str] = None,
    strip_error: bool = False,
) -> str:
    """Run bounded argv and return text, raising on a non-zero exit.

    ``error_context`` adds a stable ``"<context> exited <code>:"`` prefix.
    Omitting it preserves stderr as the complete error message. Callers whose
    wire contract excludes command line endings may request ``strip_error``.
    """

    result = run_shell_free(
        argv,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
        runner=runner,
    )
    if result.returncode:
        error = result.stderr_text(error_encoding or encoding, "replace")
        if strip_error:
            error = error.strip()
        if error_context:
            error = "{} exited {}: {}".format(
                error_context, result.returncode, error
            )
        raise error_type(error)
    return result.stdout_text(encoding, encoding_errors)


def build_i2c_argv(
    path: Mapping[str, Any],
    *,
    operation: Optional[str] = None,
    bus: Any = None,
) -> Tuple[str, ...]:
    """Build the canonical shell-free i2cget/i2cset argv."""

    operation = operation or path.get("i2c_type")
    if operation not in ("get", "set"):
        raise ValueError("I2C operation must be get or set")
    selected_bus = path.get("bus") if bus is None else bus
    executable = str(
        path.get("executable") or "/usr/sbin/i2c{}".format(operation)
    )
    argv = [executable, "-f", "-y", str(selected_bus)]
    argv.extend((str(path["chip_addr"]), str(path["command"])))
    if operation == "set":
        argv.append(str(path["value"]))
    if path.get("size") not in (None, "", "N/A"):
        argv.append(str(path["size"]))
    return tuple(argv)
