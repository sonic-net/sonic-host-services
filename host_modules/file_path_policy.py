"""Sensitive path restrictions for privileged file-read APIs."""

import errno
import os
import stat
from contextlib import contextmanager


BLOCKED_DIRECTORIES = (
    "/root",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/etc/sonic",
    "/etc/security",
    "/etc/ssl/private",
    "/etc/sudoers.d",
    "/host/glome",
)
BLOCKED_FILES = (
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/gshadow",
    "/etc/gshadow-",
    "/etc/sudoers",
)
BLOCKED_COMPONENTS = (".ssh", ".gnupg")
BLOCKED_BASENAMES = ("config_db.json", "golden_config_db.json", "minigraph.xml")


def is_sensitive_path(path):
    path = "/" + os.path.abspath(path).lstrip("/")
    # SONiC keeps the host overlay's backing files under each image's rw tree.
    parts = path.split("/")
    while (len(parts) > 4 and parts[1] == "host"
           and parts[2].startswith("image-") and parts[3] == "rw"):
        path = "/" + "/".join(parts[4:])
        parts = path.split("/")
    if any(path == blocked or path.startswith(blocked + ".")
           for blocked in BLOCKED_FILES):
        return True
    if any(path == prefix or path.startswith(prefix + "/")
           for prefix in BLOCKED_DIRECTORIES):
        return True
    if any(part in BLOCKED_COMPONENTS for part in path.split("/")):
        return True
    name = os.path.basename(path)
    return (
        any(name == blocked or name.startswith(blocked + ".")
            for blocked in BLOCKED_BASENAMES)
        or (name.startswith("ssh_host_") and not name.endswith(".pub"))
    )


def check_read_path(path):
    if is_sensitive_path(path):
        raise PermissionError(errno.EACCES, "Sensitive pathname access denied")
    resolved = os.path.realpath(path)
    if is_sensitive_path(resolved):
        raise PermissionError(errno.EACCES, "Sensitive pathname access denied")
    return resolved


@contextmanager
def open_checksum_file(path):
    resolved = check_read_path(path)
    try:
        descriptor = os.open(
            resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PermissionError(
                errno.EACCES, "Sensitive pathname access denied"
            ) from error
        raise
    with os.fdopen(descriptor, "rb") as source:
        # Check the opened object as well, not just a name that can be swapped.
        actual = os.readlink("/proc/self/fd/{}".format(source.fileno()))
        check_read_path(actual)
        info = os.fstat(source.fileno())
        # Hard links have no unique canonical name for the block-list.
        if actual.endswith(" (deleted)") or info.st_nlink != 1:
            raise PermissionError(errno.EACCES, "Ambiguous pathname access denied")
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(errno.EACCES, "Not a regular file")
        yield source
