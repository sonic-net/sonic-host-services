"""Only published managed images are eligible for privileged read APIs."""

from host_modules.image_store import ImageStore, artifact_parts


_store = ImageStore()


def check_read_path(path):
    artifact_parts(path)
    return path


def open_checksum_file(path):
    check_read_path(path)
    return _store.open(path)
