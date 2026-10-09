"""Small, durable filesystem primitives shared by DLDD components."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any, Callable, Dict, IO, Mapping


def _fsync_directory(directory: str) -> None:
    """Persist directory-entry changes made by an atomic replacement."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _destination(path: str) -> tuple[str, str]:
    destination = os.path.abspath(path)
    directory = os.path.dirname(destination)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    return destination, directory


def _atomic_replace(
    path: str, mode: str, writer: Callable[[IO[Any]], None]
) -> None:
    """Write and durably replace one destination through a sibling file."""

    destination, directory = _destination(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".dldd-", dir=directory)
    try:
        if "b" in mode:
            stream = os.fdopen(descriptor, mode)
        else:
            stream = os.fdopen(descriptor, mode, encoding="utf-8")
        descriptor = -1
        with stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        unlink_if_exists(temporary)


def atomic_write_json(path: str, document: Mapping[str, Any]) -> None:
    """Atomically write a JSON object and durably publish its directory entry."""

    _atomic_replace(
        path,
        "w",
        lambda stream: json.dump(document, stream, sort_keys=True, indent=2),
    )


def atomic_copy(source_path: str, destination_path: str) -> None:
    """Atomically copy a regular file and durably publish the destination."""

    def copy_to(destination_stream: IO[Any]) -> None:
        with open(source_path, "rb") as source_stream:
            shutil.copyfileobj(source_stream, destination_stream, 1024 * 1024)

    _atomic_replace(destination_path, "wb", copy_to)


def unlink_if_exists(path: str) -> bool:
    """Unlink one path, returning whether an entry was removed."""

    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def load_json_object(path: str) -> Dict[str, Any]:
    """Load a JSON object, returning an empty object for absent or invalid data."""

    try:
        with open(path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}
