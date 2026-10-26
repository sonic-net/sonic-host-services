"""Private managed-image storage with kernel-enforced, unprivileged access."""

import array
import errno
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager


SERVICE_USER = "sonic-image"
STORE_ROOT = "/host/sonic-image-exports"
EXPORT_ROOT = STORE_ROOT + "/images"
DENIED = "Image artifact access denied"
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")
logger = logging.getLogger(__name__)


def artifact_parts(path, allow_directory=False):
    if not isinstance(path, str) or not path.startswith("/"):
        raise PermissionError(errno.EACCES, DENIED)
    if ".." in path.split("/") or "\0" in path:
        raise PermissionError(errno.EACCES, DENIED)
    path = "/" + os.path.normpath(path).lstrip("/")
    if path == EXPORT_ROOT and allow_directory:
        return []
    if not path.startswith(EXPORT_ROOT + "/"):
        raise PermissionError(errno.EACCES, DENIED)
    parts = path[len(EXPORT_ROOT) + 1:].split("/")
    if len(parts) > 16 or any(not _COMPONENT.fullmatch(part) for part in parts):
        raise PermissionError(errno.EACCES, DENIED)
    return parts


def _directory(path, dir_fd=None):
    return os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )


def _check_owned(descriptor, uid, gid, mode, directory=False):
    info = os.fstat(descriptor)
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid_type or info.st_uid != uid or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != mode
            or (not directory and info.st_nlink != 1)):
        raise PermissionError(errno.EACCES, DENIED)


class ImageStore:
    def __init__(self, host_root="/"):
        self.host_root = os.path.realpath(host_root)

    def _physical(self, path):
        return os.path.join(self.host_root, path.lstrip("/"))

    def credentials(self):
        with open(self._physical("/etc/passwd"), encoding="utf-8") as records:
            matches = [line.rstrip("\n").split(":") for line in records
                       if line.startswith(SERVICE_USER + ":")]
        if len(matches) != 1 or len(matches[0]) != 7:
            raise OSError(errno.ENXIO, "Managed-image service account is unavailable")
        try:
            uid, gid = int(matches[0][2]), int(matches[0][3])
        except ValueError as error:
            raise OSError(errno.ENXIO, "Invalid managed-image service identity") from error
        if uid <= 0 or gid <= 0 or matches[0][6] != "/usr/sbin/nologin":
            raise OSError(errno.ENXIO, "Invalid managed-image service account")
        return uid, gid

    def prepare(self):
        uid, gid = self.credentials()
        if os.geteuid() != 0:
            raise PermissionError(errno.EACCES, "Image-store provisioning requires root")
        parent = _directory(self._physical("/host"))
        try:
            info = os.fstat(parent)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise PermissionError(errno.EACCES, "Unsafe image-store parent directory")
            self._ensure_directory(parent, "sonic-image-exports", 0, 0, 0o755)
            base = _directory("sonic-image-exports", parent)
            try:
                for name in ("images", "staging"):
                    self._ensure_directory(base, name, uid, gid, 0o700)
            finally:
                os.close(base)
        finally:
            os.close(parent)

    @staticmethod
    def _ensure_directory(parent, name, uid, gid, mode):
        created = False
        try:
            os.mkdir(name, mode, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        descriptor = _directory(name, parent)
        try:
            if created:
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, mode)
            _check_owned(descriptor, uid, gid, mode, directory=True)
        finally:
            os.close(descriptor)

    def _request(self, operation, path, token=""):
        artifact_parts(path, allow_directory=operation == "open")
        uid, gid = self.credentials()
        received = []
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        process = None
        try:
            parent.settimeout(30)
            process = subprocess.Popen(
                [sys.executable, "-m", "host_modules.image_file_worker",
                 str(child.fileno()), self.host_root, operation, path, token],
                pass_fds=(child.fileno(),), user=uid, group=gid, extra_groups=[],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            child.close()
            payload, ancillary, flags, _ = parent.recvmsg(
                128, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC
            )
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors = array.array("i")
                    descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
                    received.extend(descriptors)
            _, stderr = process.communicate(timeout=30)
            if payload == b"DENIED":
                raise PermissionError(errno.EACCES, DENIED)
            needs_fd = operation in ("open", "create")
            if (process.returncode or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                    or not payload.startswith(b"OK")
                    or len(received) != int(needs_fd)):
                logger.error("Managed-image worker failed: %s", stderr.decode(errors="replace"))
                raise OSError(errno.EIO, "Managed-image worker failed")
            result_token = payload[2:].decode("ascii").strip()
            if operation == "create" and not _TOKEN.fullmatch(result_token):
                raise OSError(errno.EIO, "Invalid managed-image staging response")
            descriptor = received.pop() if needs_fd else None
            if descriptor is not None:
                os.set_inheritable(descriptor, False)
            return descriptor, result_token
        finally:
            parent.close()
            child.close()
            for descriptor in received:
                os.close(descriptor)
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    @contextmanager
    def open(self, path):
        descriptor, _ = self._request("open", path)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise PermissionError(errno.EACCES, DENIED)
            yield source

    @contextmanager
    def create(self, path):
        artifact_parts(path)
        self.prepare()
        descriptor, token = self._request("create", path)
        committed = False
        try:
            with os.fdopen(descriptor, "wb") as target:
                yield target
                target.flush()
                os.fsync(target.fileno())
            self._request("publish", path, token)
            committed = True
        finally:
            if not committed:
                try:
                    self._request("abort", path, token)
                except OSError:
                    logger.exception("Failed to remove managed-image staging artifact")


def worker_operation(host_root, operation, path, token):
    """Run only after the parent has dropped all root and supplementary IDs."""
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0 or gid == 0 or os.getuid() != uid or os.getgroups():
        raise PermissionError(errno.EACCES, DENIED)
    parts = artifact_parts(path, allow_directory=operation == "open")
    store = ImageStore(host_root)
    images = _directory(store._physical(EXPORT_ROOT))
    try:
        _check_owned(images, uid, gid, 0o700, directory=True)
        if operation == "open":
            return _open_artifact(images, parts, uid, gid), ""
        if operation not in ("create", "publish", "abort"):
            raise PermissionError(errno.EACCES, DENIED)
        staging = _directory(store._physical(STORE_ROOT + "/staging"))
        try:
            _check_owned(staging, uid, gid, 0o700, directory=True)
            if operation == "create":
                token = uuid.uuid4().hex
                descriptor = os.open(
                    token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                    | os.O_CLOEXEC, 0o600, dir_fd=staging,
                )
                os.fchmod(descriptor, 0o600)
                return descriptor, token
            if not _TOKEN.fullmatch(token):
                raise PermissionError(errno.EACCES, DENIED)
            if operation == "abort":
                try:
                    os.unlink(token, dir_fd=staging)
                except FileNotFoundError:
                    pass
                return None, ""
            source = os.open(token, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=staging)
            try:
                _check_owned(source, uid, gid, 0o600)
            finally:
                os.close(source)
            destination = _artifact_parent(images, parts[:-1], uid, gid, create=True)
            try:
                os.replace(token, parts[-1], src_dir_fd=staging, dst_dir_fd=destination)
            finally:
                os.close(destination)
            return None, ""
        finally:
            os.close(staging)
    finally:
        os.close(images)


def _artifact_parent(images, parts, uid, gid, create=False):
    current = os.dup(images)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            following = _directory(part, current)
            try:
                _check_owned(following, uid, gid, 0o700, directory=True)
            except PermissionError:
                os.close(following)
                raise
            os.close(current)
            current = following
        result, current = current, None
        return result
    finally:
        if current is not None:
            os.close(current)


def _open_artifact(images, parts, uid, gid):
    if not parts:
        return os.dup(images)
    parent = _artifact_parent(images, parts[:-1], uid, gid)
    try:
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(source)
        _check_owned(source, uid, gid, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600,
                     directory=stat.S_ISDIR(info.st_mode))
    except PermissionError:
        os.close(source)
        raise
    return source
