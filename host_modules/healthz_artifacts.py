"""Host-owned, bounded Healthz archives built from caller-supplied files."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import tarfile
import tempfile
import threading
import time
import uuid


ARTIFACT_DIRECTORY = "/var/lib/sonic/healthz/artifacts"
ARTIFACT_NAME = re.compile(r"healthz-[0-9a-f]{32}\.tar\.gz\Z")
MAX_REQUEST_BYTES = 64 * 1024
MAX_PATHS = 128
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_ARTIFACTS = 20
PENDING_SECONDS = 24 * 60 * 60


def _file_info(path):
    """Open a regular absolute file without following any path symlink."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("artifact source must be an absolute path")
    parts = path.split("/")[1:]
    if not parts or any(part in ("", "..") for part in parts):
        raise ValueError("artifact source has an invalid path component")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                            os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW |
                                  os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("artifact source is not a regular file")
    except Exception:
        os.close(file_descriptor)
        raise
    return file_descriptor, info


def _archive_name(name):
    if (not isinstance(name, str) or not name or len(name) > 512
            or "\\" in name or "\x00" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("invalid archive member name")
    return name


class HealthzArtifacts:
    """Reserve a stable ID, then atomically publish an archive from files."""

    def __init__(self, directory=ARTIFACT_DIRECTORY,
                 max_artifacts=MAX_ARTIFACTS,
                 max_bytes=MAX_ARTIFACT_BYTES):
        self.directory = os.fspath(directory)
        self.max_artifacts = max_artifacts
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        info = os.lstat(self.directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Healthz artifact directory must be owned by the service")
        os.chmod(self.directory, 0o700)

    def _archive(self, artifact_id):
        if not isinstance(artifact_id, str) or not ARTIFACT_NAME.fullmatch(artifact_id):
            raise ValueError("invalid Healthz artifact ID")
        return os.path.join(self.directory, artifact_id)

    def _pending(self, artifact_id):
        return os.path.join(self.directory, "." + artifact_id + ".pending")

    def _state(self, artifact_id):
        archive = self._archive(artifact_id)
        try:
            if stat.S_ISREG(os.lstat(archive).st_mode):
                return "COMPLETED"
        except FileNotFoundError:
            pass
        try:
            marker = os.lstat(self._pending(artifact_id))
            if (stat.S_ISREG(marker.st_mode)
                    and time.time() - marker.st_mtime < PENDING_SECONDS):
                return "PENDING"
        except FileNotFoundError:
            pass
        return "MISSING"

    def status(self, artifact_id):
        with self._lock:
            return self._state(artifact_id)

    def _fsync_directory(self):
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY |
                             os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _prune(self):
        now = time.time()
        archives = []
        markers = []
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            info = os.lstat(path)
            if (name.startswith(".healthz-") and name.endswith(".pending")
                    and stat.S_ISREG(info.st_mode)):
                if now - info.st_mtime >= PENDING_SECONDS:
                    os.unlink(path)
                else:
                    markers.append((name[1:-len(".pending")], path))
            elif ARTIFACT_NAME.fullmatch(name) and stat.S_ISREG(info.st_mode):
                archives.append((info.st_mtime, path))
            elif name.startswith(".healthz-") and name.endswith(".tmp"):
                if stat.S_ISREG(info.st_mode) and now - info.st_mtime >= PENDING_SECONDS:
                    os.unlink(path)
        archive_ids = {os.path.basename(path) for _, path in archives}
        pending = 0
        for artifact_id, marker in markers:
            if artifact_id in archive_ids:
                os.unlink(marker)
            else:
                pending += 1
        archives.sort()
        for _, path in archives[:max(0, len(archives) + pending + 1 - self.max_artifacts)]:
            os.unlink(path)
        if pending >= self.max_artifacts:
            raise OSError("Healthz artifact capacity is exhausted")

    def reserve(self):
        with self._lock:
            self._prune()
            requested_at = int(time.time())
            while True:
                artifact_id = "healthz-{}.tar.gz".format(uuid.uuid4().hex)
                if self._state(artifact_id) != "MISSING":
                    continue
                try:
                    descriptor = os.open(self._pending(artifact_id),
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                         os.O_NOFOLLOW, 0o600)
                    break
                except FileExistsError:
                    continue
            with os.fdopen(descriptor, "w") as marker:
                marker.write(str(requested_at))
                marker.flush()
                os.fsync(marker.fileno())
            self._fsync_directory()
            return {"artifact_id": artifact_id, "requested_at": requested_at,
                    "location": self._archive(artifact_id)}

    def submit(self, artifact_id, paths, metadata=None):
        if not isinstance(paths, list) or len(paths) > MAX_PATHS:
            raise ValueError("paths must be a list of at most 128 files")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        names = set()
        for item in paths:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("each path must have a source and archive name")
            name = _archive_name(item.get("name"))
            if name in names or (metadata is not None and name == "metadata.json"):
                raise ValueError("duplicate archive member name")
            names.add(name)
        with self._lock:
            archive_path = self._archive(artifact_id)
            if self._state(artifact_id) == "COMPLETED":
                return {"artifact_id": artifact_id, "location": archive_path}
            if self._state(artifact_id) != "PENDING":
                raise FileNotFoundError("Healthz artifact reservation not found")
            descriptor, staged = tempfile.mkstemp(
                prefix="." + artifact_id[:-7] + "-", suffix=".tmp",
                dir=self.directory)
            os.close(descriptor)
            try:
                total = 0
                with tarfile.open(staged, "w:gz", compresslevel=1) as archive:
                    if metadata is not None:
                        content = json.dumps(metadata, sort_keys=True).encode("utf-8")
                        total += len(content)
                        if total > self.max_bytes:
                            raise ValueError("artifact exceeds size limit")
                        entry = tarfile.TarInfo("metadata.json")
                        entry.size = len(content)
                        entry.mtime = int(time.time())
                        entry.mode = 0o600
                        archive.addfile(entry, io.BytesIO(content))
                    for item in paths:
                        source, info = _file_info(item["path"])
                        with os.fdopen(source, "rb") as stream:
                            if total + info.st_size > self.max_bytes:
                                raise ValueError("artifact exceeds size limit")
                            entry = tarfile.TarInfo(item["name"])
                            entry.size = info.st_size
                            entry.mtime = int(info.st_mtime)
                            entry.mode = 0o600
                            archive.addfile(entry, stream)
                            total += info.st_size
                if os.path.getsize(staged) > self.max_bytes:
                    raise ValueError("compressed artifact exceeds size limit")
                with open(staged, "rb") as stream:
                    os.fsync(stream.fileno())
                try:
                    os.link(staged, archive_path, follow_symlinks=False)
                except FileExistsError:
                    if self._state(artifact_id) != "COMPLETED":
                        raise
                else:
                    self._fsync_directory()
                    os.unlink(self._pending(artifact_id))
                    self._fsync_directory()
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
            return {"artifact_id": artifact_id, "location": archive_path}

    def fail(self, artifact_id):
        with self._lock:
            if self._state(artifact_id) == "COMPLETED":
                return
            try:
                os.unlink(self._pending(artifact_id))
            except FileNotFoundError:
                pass
            self._fsync_directory()
