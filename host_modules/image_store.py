"""Private managed-image storage with kernel-enforced, unprivileged access."""

import array
import errno
import fcntl
import json
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
        if not (0 < uid < 2**32 - 1 and 0 < gid < 2**32 - 1) or matches[0][6] != "/usr/sbin/nologin":
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
                _prepare_ownership(base, uid, gid)
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
        if operation != "abort":
            self.prepare()
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


def _root_record(base, name, flags):
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         0o600, dir_fd=base)
    try:
        _check_owned(descriptor, 0, 0, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_ownership(base):
    with os.fdopen(_root_record(base, "ownership.json", os.O_RDONLY), "rb") as source:
        data = source.read(513)
    try:
        record = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise OSError(errno.EINVAL, "Invalid managed-image ownership journal") from error
    keys = {"version", "uid", "gid", "next_uid", "next_gid"}
    if (len(data) > 512 or not isinstance(record, dict) or set(record) != keys
            or any(type(record[key]) is not int for key in keys)
            or record["version"] != 1
            or not all(0 < record[key] < 2**32 - 1 for key in ("uid", "gid"))
            or not all(0 <= record[key] < 2**32 - 1 for key in ("next_uid", "next_gid"))
            or bool(record["next_uid"]) != bool(record["next_gid"])):
        raise OSError(errno.EINVAL, "Invalid managed-image ownership journal")
    return record


def _write_ownership(base, record):
    name = ".ownership-" + uuid.uuid4().hex
    descriptor = _root_record(base, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as target:
            json.dump(record, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, "ownership.json", src_dir_fd=base, dst_dir_fd=base)
        os.fsync(base)
    finally:
        try:
            os.unlink(name, dir_fd=base)
        except FileNotFoundError:
            pass


def _walk_ownership(parent, name, record, migrate, staging, depth=0):
    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        def validate():
            info = os.fstat(descriptor)
            owner = (info.st_uid, info.st_gid)
            known = owner == (record["uid"], record["gid"]) or (
                record["next_uid"] != 0
                and owner == (record["next_uid"], record["next_gid"]))
            directory = stat.S_ISDIR(info.st_mode)
            if (not known or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                    or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
                    or depth > 16 or (depth == 0 and not directory)
                    or (staging and depth != 0 and directory)):
                raise PermissionError(errno.EACCES, DENIED)
            return directory

        directory = validate()
        # Inspect type before opening; do not invoke a device's open as root.
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
        if directory:
            flags |= os.O_DIRECTORY
        opened = os.open(f"/proc/self/fd/{descriptor}", flags)
        os.close(descriptor)
        descriptor = opened
        if directory:
            for child in os.listdir(descriptor):
                if (not _COMPONENT.fullmatch(child)
                        or (staging and not _TOKEN.fullmatch(child))):
                    raise PermissionError(errno.EACCES, DENIED)
                _walk_ownership(descriptor, child, record, migrate, staging, depth + 1)
        if migrate:
            validate()
            os.fchown(descriptor, record["next_uid"], record["next_gid"])
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ownership_trees(base, record, migrate=False):
    for name in ("images", "staging"):
        _walk_ownership(base, name, record, migrate, name == "staging")


def _prepare_ownership(base, uid, gid):
    with os.fdopen(_root_record(base, "ownership.lock", os.O_RDWR | os.O_CREAT), "r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            record = _read_ownership(base)
        except FileNotFoundError:
            record = None
        missing = record is None
        if missing:
            record = {"version": 1, "uid": uid, "gid": gid, "next_uid": 0, "next_gid": 0}
        for name in ("images", "staging"):
            try:
                os.mkdir(name, 0o700, dir_fd=base)
            except FileExistsError:
                continue
            descriptor = _directory(name, base)
            try:
                os.fchown(descriptor, record["uid"], record["gid"])
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if missing:
            _ownership_trees(base, record)
            _write_ownership(base, record)
        while record["next_uid"] or (record["uid"], record["gid"]) != (uid, gid):
            if not record["next_uid"]:
                _ownership_trees(base, record)
                record.update(next_uid=uid, next_gid=gid)
                _write_ownership(base, record)
            _ownership_trees(base, record)
            _ownership_trees(base, record, migrate=True)
            record.update(uid=record["next_uid"], gid=record["next_gid"],
                          next_uid=0, next_gid=0)
            _write_ownership(base, record)
        for name in ("images", "staging"):
            descriptor = _directory(name, base)
            try:
                _check_owned(descriptor, uid, gid, 0o700, directory=True)
            finally:
                os.close(descriptor)


def worker_operation(host_root, operation, path, token):
    """Run only after the parent has dropped all root and supplementary IDs."""
    uid, gid = os.geteuid(), os.getegid()
    if uid == 0 or gid == 0 or os.getuid() != uid or os.getgroups():
        raise PermissionError(errno.EACCES, DENIED)
    parts = artifact_parts(path, allow_directory=operation == "open")
    store = ImageStore(host_root)
    if operation == "abort":
        if not _TOKEN.fullmatch(token):
            raise PermissionError(errno.EACCES, DENIED)
        staging = _directory(store._physical(STORE_ROOT + "/staging"))
        try:
            _check_owned(staging, uid, gid, 0o700, directory=True)
            try:
                os.unlink(token, dir_fd=staging)
            except FileNotFoundError:
                pass
            return None, ""
        finally:
            os.close(staging)
    images = _directory(store._physical(EXPORT_ROOT))
    try:
        _check_owned(images, uid, gid, 0o700, directory=True)
        if operation == "open":
            return _open_artifact(images, parts, uid, gid), ""
        if operation not in ("create", "publish"):
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
