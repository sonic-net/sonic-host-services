"""Public-read permission checks for privileged local-image reads."""

import errno
import os
import stat
from collections import deque
from contextlib import contextmanager


DENIED = "File read access denied"
MAX_READ_PATH_LENGTH = 4096


def _check_public(descriptor, required):
    if os.fstat(descriptor).st_mode & required != required:
        raise PermissionError(errno.EACCES, DENIED)
    # O_PATH descriptors support search-only directories. Inspect ACLs through
    # the pinned inode, since fgetxattr does not accept O_PATH descriptors.
    try:
        os.getxattr(f"/proc/self/fd/{descriptor}", "system.posix_acl_access")
    except OSError as error:
        if error.errno in (errno.ENODATA, errno.ENOTSUP):
            return
        raise
    raise PermissionError(errno.EACCES, DENIED)


@contextmanager
def open_checksum_file(path):
    if (not isinstance(path, str) or len(path) >= MAX_READ_PATH_LENGTH
            or len(os.fsencode(path)) >= MAX_READ_PATH_LENGTH
            or not os.path.isabs(path) or "\0" in path):
        raise PermissionError(errno.EACCES, DENIED)
    directories = [os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)]
    pending = deque(path.split("/"))
    links = 0
    try:
        while pending:
            part = pending.popleft()
            if part in ("", "."):
                continue
            _check_public(directories[-1], stat.S_IXOTH)
            if part == "..":
                if len(directories) == 1:
                    raise PermissionError(errno.EACCES, DENIED)
                os.close(directories.pop())
                continue
            descriptor = os.open(
                part, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directories[-1],
            )
            try:
                mode = os.fstat(descriptor).st_mode
                if stat.S_ISLNK(mode):
                    links += 1
                    if links > 40:
                        raise PermissionError(errno.EACCES, DENIED)
                    target = os.readlink("", dir_fd=descriptor)
                    if os.path.isabs(target):
                        while len(directories) > 1:
                            os.close(directories.pop())
                    pending.extendleft(reversed(target.split("/")))
                elif stat.S_ISDIR(mode):
                    directories.append(descriptor)
                    descriptor = None
                else:
                    if pending or not stat.S_ISREG(mode):
                        raise PermissionError(errno.EACCES, DENIED)
                    _check_public(descriptor, stat.S_IROTH)
                    source_fd = os.open(
                        f"/proc/self/fd/{descriptor}",
                        os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC,
                    )
                    try:
                        source = os.fdopen(source_fd, "rb")
                    except BaseException:
                        os.close(source_fd)
                        raise
                    with source:
                        yield source
                    return
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        raise PermissionError(errno.EACCES, DENIED)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT,
                           errno.ENOTDIR, errno.ELOOP, errno.ENXIO):
            raise PermissionError(errno.EACCES, DENIED) from error
        raise
    finally:
        for directory in directories:
            os.close(directory)
