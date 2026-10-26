"""Unprivileged managed-image worker; descriptors travel over a private socket."""

import array
import errno
import logging
import os
import socket
import sys

from host_modules.image_store import worker_operation


def main():
    descriptor = None
    with socket.socket(fileno=int(sys.argv[1])) as channel:
        try:
            descriptor, token = worker_operation(*sys.argv[2:])
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT,
                               errno.ENOTDIR, errno.ELOOP, errno.ENXIO):
                channel.send(b"DENIED")
                return 1
            logging.exception("Managed-image operation failed")
            channel.send(b"ERROR")
            return 1
        try:
            ancillary = []
            if descriptor is not None:
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                              array.array("i", [descriptor]))]
            channel.sendmsg([("OK " + token).encode("ascii")], ancillary)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
