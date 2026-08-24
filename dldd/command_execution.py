"""Shell-free subprocess and i2c-tools command construction helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Type


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


def _bounded_bytes(value, limit: int) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value[:limit]
    return str(value).encode("utf-8", "replace")[:limit]


@dataclass(frozen=True)
class ShellFreeResult:
    argv: Tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    def stdout_text(self, encoding="utf-8", errors="replace") -> str:
        return self.stdout.decode(encoding, errors)

    def stderr_text(self, encoding="utf-8", errors="replace") -> str:
        return self.stderr.decode(encoding, errors)


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
    completed = (runner or subprocess.run)(
        list(command),
        shell=False,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return ShellFreeResult(
        command,
        int(completed.returncode),
        _bounded_bytes(completed.stdout, max_output_bytes),
        _bounded_bytes(completed.stderr, max_output_bytes),
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
    argv = [
        executable,
        "-f",
        "-y",
        str(selected_bus),
        str(path["chip_addr"]),
        str(path["command"]),
    ]
    if operation == "set":
        argv.append(str(path["value"]))
    if path.get("size") not in (None, "", "N/A"):
        argv.append(str(path["size"]))
    return tuple(argv)
