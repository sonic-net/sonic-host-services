import errno
import hashlib
import os
import stat
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from host_modules import file_path_policy, image_service
from host_modules.file_path_policy import DENIED, open_checksum_file


@pytest.fixture
def public_root(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="public-image-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o755)
        monkeypatch.setattr(image_service, "INSTALL_STAGING_PARENT", directory)
        yield root


@pytest.fixture
def service():
    with mock.patch("dbus.SystemBus"), mock.patch("dbus.service.BusName"), \
            mock.patch("dbus.service.Object.__init__"):
        yield image_service.ImageService(mod_name="image_service")


def assert_denied(service, path):
    assert service.checksum(str(path), "sha256") == (errno.EACCES, DENIED)
    with mock.patch("subprocess.run", side_effect=AssertionError("privileged installer")):
        assert service.install(str(path)) == (errno.EACCES, DENIED)


def assert_checksum(service, path, content=b"public image"):
    assert service.checksum(str(path), "sha256") == (
        0, hashlib.sha256(content).hexdigest()
    )


def set_deny_acl(path, directory=False):
    owner, other = (7, 5) if directory else (6, 4)
    entries = [(1, owner, 0xFFFFFFFF), (2, 0, 65534), (4, other, 0xFFFFFFFF),
               (16, other, 0xFFFFFFFF), (32, other, 0xFFFFFFFF)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(path, "system.posix_acl_access", acl)
    except OSError as error:
        if error.errno == errno.ENOTSUP:
            pytest.skip("test filesystem has no POSIX ACL support")
        raise


@pytest.mark.parametrize("mode", [0o644, 0o444, 0o666, 0o600, 0o640, 0o660, 0o400, 0o000])
def test_local_read_permissions(service, public_root, mode):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(mode)
    if mode & stat.S_IROTH:
        assert_checksum(service, path)
    else:
        assert_denied(service, path)


@pytest.mark.parametrize("suffix", ["", "/child"])
def test_missing_private_and_non_directory_denials_match(service, public_root, suffix):
    private = public_root / "private"
    private.write_bytes(b"private")
    private.chmod(0o600)
    assert_denied(service, str(private) + suffix)
    assert_denied(service, str(public_root / "missing") + suffix)


@pytest.mark.parametrize("path", [
    "/" * 4096, "/" * (128 * 1024), "/" + "\u00e9" * 2048,
], ids=["at-limit", "large", "utf8-byte-limit"])
def test_overlong_paths_are_denied_before_filesystem_access(service, path):
    with mock.patch.object(file_path_policy.os, "open", side_effect=AssertionError("filesystem access")):
        assert_denied(service, path)


@pytest.mark.parametrize("name", ["image", "\u00e9"], ids=["ascii", "utf8"])
def test_longest_supported_raw_path(service, public_root, name):
    path = public_root / name
    path.write_bytes(b"public image")
    path.chmod(0o644)
    requested = "/" * (4095 - len(os.fsencode(path))) + str(path)
    assert len(os.fsencode(requested)) == 4095
    assert_checksum(service, requested)


@pytest.mark.parametrize("name", ["a" * 255, "\u00e9" * 127 + "a"], ids=["ascii", "utf8"])
def test_component_length_denials(service, public_root, name):
    path = public_root / name
    path.write_bytes(b"public image")
    path.chmod(0o644)
    assert_checksum(service, path)
    assert_denied(service, str(path) + "a")


@pytest.mark.parametrize("kind", ["file", "relative", "absolute"])
def test_directory_budget_exact_limit(service, public_root, kind):
    current = public_root
    for _ in range(64 - len(public_root.parts)):
        current = current / "d"
        current.mkdir(mode=0o755)
    image = current / "image"
    image.write_bytes(b"public image")
    image.chmod(0o644)
    requested = image
    if kind != "file":
        requested = current / "link"
        requested.symlink_to("image" if kind == "relative" else image)
    assert_checksum(service, requested)
    with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stderr=b"")):
        assert service.install(str(requested)) == (0, "")
    beyond = current / "beyond"
    beyond.mkdir(mode=0o755)
    blocked = beyond / "image"
    blocked.write_bytes(b"public image")
    blocked.chmod(0o644)
    assert_denied(service, blocked)


def test_directory_budget_preserves_descriptor_headroom(service, public_root):
    directories, files = [], []
    current = public_root
    healthy = public_root / "healthy"
    healthy.write_bytes(b"public image")
    healthy.chmod(0o644)
    edge = None
    try:
        for _ in range(1050):
            current = current / "d"
            current.mkdir(mode=0o755)
            directories.append(current)
            if len(current.parts) == 64:
                edge = current / "edge"
                edge.write_bytes(b"public image")
                edge.chmod(0o644)
                files.append(edge)
        image, private = current / "image", current / "private"
        for path, mode in [(image, 0o644), (private, 0o600)]:
            path.write_bytes(b"public image")
            path.chmod(mode)
            files.append(path)
        script = """
import errno, os, resource, sys
from host_modules.file_path_policy import DENIED, open_checksum_file
resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
deep, healthy, edge = sys.argv[1:]
with open(deep, "rb") as source:
    assert source.read() == b"public image"
baseline = len(os.listdir("/proc/self/fd"))
with open_checksum_file(healthy) as source:
    assert len(os.listdir("/proc/self/fd")) <= baseline + 2
    assert source.read() == b"public image"
with open_checksum_file(edge) as source:
    assert len(os.listdir("/proc/self/fd")) <= baseline + 2
    assert source.read() == b"public image"
before = len(os.listdir("/proc/self/fd"))
for _ in range(20):
    try:
        with open_checksum_file(deep):
            raise AssertionError("unbounded directory traversal")
    except PermissionError as error:
        assert error.errno == errno.EACCES and error.strerror == DENIED
assert len(os.listdir("/proc/self/fd")) == before
with open(healthy, "rb") as source:
    assert source.read() == b"public image"
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(image), str(healthy), str(edge)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert_denied(service, image)
        assert_denied(service, private)
        assert_denied(service, current / "missing")
        assert_checksum(service, healthy)
    finally:
        for path in files:
            path.unlink()
        for directory in reversed(directories):
            directory.rmdir()


def test_private_and_search_only_ancestors(service, public_root):
    parent = public_root / "parent"
    parent.mkdir(mode=0o755)
    image = parent / "image"
    image.write_bytes(b"public image")
    image.chmod(0o644)
    assert_checksum(service, image)
    try:
        parent.chmod(0o111)
        assert image.read_bytes() == b"public image"
        assert_checksum(service, image)
        parent.chmod(0o700)
        assert_denied(service, image)
        assert_denied(service, parent / "missing")
    finally:
        parent.chmod(0o755)


def test_symlink_targets_and_original_ancestors(service, public_root):
    image = public_root / "image"
    image.write_bytes(b"public image")
    image.chmod(0o644)
    private = public_root / "private"
    private.mkdir(mode=0o755)
    link = private / "link"
    link.symlink_to("../image")
    assert_checksum(service, link)
    private.chmod(0o700)
    assert_denied(service, link)
    assert_denied(service, str(private) + "/../image")
    detour = public_root / "detour"
    detour.symlink_to("private/../image")
    assert_denied(service, detour)
    private.chmod(0o755)
    assert_checksum(service, detour)
    image.chmod(0o600)
    assert_denied(service, link)
    absolute = public_root / "absolute"
    absolute.symlink_to(image)
    assert_denied(service, absolute)
    image.chmod(0o644)
    assert_checksum(service, absolute)


@pytest.mark.parametrize("target", ["file", "ancestor", "symlink-ancestor"])
def test_acl_denial_has_positive_controls(service, public_root, target):
    image = public_root / "image"
    image.write_bytes(b"public image")
    image.chmod(0o644)
    acl_path = image
    requested = image
    if target == "ancestor":
        acl_path = public_root
    elif target == "symlink-ancestor":
        acl_path = public_root / "ancestor"
        acl_path.mkdir(mode=0o755)
        requested = acl_path / "link"
        requested.symlink_to("../image")
    assert_checksum(service, requested)
    set_deny_acl(acl_path, directory=target != "file")
    assert_denied(service, requested)
    os.removexattr(acl_path, "system.posix_acl_access")
    assert_checksum(service, requested)


@pytest.mark.parametrize("kind", ["directory", "fifo", "loop", "relative", "file-url"])
def test_unsupported_local_reads(service, public_root, kind):
    path = public_root / "source"
    if kind == "directory":
        path.mkdir(mode=0o755)
    elif kind == "fifo":
        os.mkfifo(path, 0o644)
    elif kind == "loop":
        path.symlink_to("source")
    elif kind == "relative":
        path = "relative/image"
    else:
        path = "file:///etc/shadow"
    assert_denied(service, path)


@pytest.mark.parametrize("returncode", [0, 1])
def test_local_install_uses_private_copy_and_cleans_up(service, public_root, returncode):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    installed = []

    def install(command, **kwargs):
        copy = Path(command[-1])
        installed.append(copy)
        assert copy != path
        assert copy.read_bytes() == b"public image"
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
        return mock.Mock(returncode=returncode, stderr=b"Error: synthetic failure")

    with mock.patch("subprocess.run", side_effect=install):
        assert service.install(str(path))[0] == returncode
    assert len(installed) == 1 and not installed[0].parent.exists()


@pytest.mark.parametrize("operation", ["checksum", "install"])
def test_replacement_after_validation_cannot_change_read(service, public_root, operation):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    replacement = public_root / "replacement"
    replacement.write_bytes(b"private replacement")
    replacement.chmod(0o600)

    @contextmanager
    def replace(name):
        with open_checksum_file(name) as source:
            os.replace(replacement, path)
            yield source

    with mock.patch.object(image_service, "open_checksum_file", replace):
        if operation == "checksum":
            assert_checksum(service, path)
        else:
            def install(command, **kwargs):
                assert Path(command[-1]).read_bytes() == b"public image"
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch("subprocess.run", side_effect=install):
                assert service.install(str(path)) == (0, "")
    assert list(public_root.glob(".sonic-image-install-*")) == []


@pytest.mark.parametrize("code", [
    errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR,
    errno.ELOOP, errno.ENXIO, errno.ENAMETOOLONG, errno.EIO,
])
def test_caller_body_errors_propagate_unchanged(public_root, code):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    failure = OSError(code, "caller operation failed")
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(OSError) as caught:
        with open_checksum_file(str(path)) as source:
            assert source.read() == b"public image"
            raise failure
    assert caught.value is failure
    assert source.closed
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("operation", ["installer", "staging", "copy"])
def test_operational_missing_file_is_not_access_denial(service, public_root, monkeypatch, caplog, operation):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    installer = mock.Mock(return_value=mock.Mock(returncode=0, stderr=b""))
    failure = FileNotFoundError(errno.ENOENT, "missing operational dependency")
    if operation == "installer":
        installer.side_effect = failure
    elif operation == "staging":
        monkeypatch.setattr(image_service, "INSTALL_STAGING_PARENT", str(public_root / "missing"))
    else:
        monkeypatch.setattr(image_service.shutil, "copyfileobj", mock.Mock(side_effect=failure))
    monkeypatch.setattr(image_service.subprocess, "run", installer)
    before = len(os.listdir("/proc/self/fd"))
    assert service.install(str(path)) == (errno.EIO, "Image installation failed")
    assert "Image installation failed" in caplog.text
    assert "Denied installation" not in caplog.text
    if operation == "installer":
        installer.assert_called_once()
    else:
        installer.assert_not_called()
    assert list(public_root.glob(".sonic-image-install-*")) == []
    assert len(os.listdir("/proc/self/fd")) == before


def test_copy_failure_cleans_up_without_running_installer(service, public_root):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    with mock.patch("shutil.copyfileobj", side_effect=OSError(errno.ENOSPC, "disk full")), \
            mock.patch("subprocess.run") as installer:
        assert service.install(str(path)) == (errno.EIO, "Image installation failed")
    installer.assert_not_called()
    assert list(public_root.glob(".sonic-image-install-*")) == []


@pytest.mark.parametrize("operation", ["checksum", "install"])
def test_descriptor_construction_failure_closes_all_descriptors(service, public_root, operation):
    path = public_root / "image"
    path.write_bytes(b"public image")
    path.chmod(0o644)
    opened = []
    real_open = os.open
    real_fdopen = os.fdopen

    def capture(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def construct(descriptor, mode, *args, **kwargs):
        if operation == "checksum" or mode == "wb":
            raise OSError(errno.EIO, "synthetic failure")
        return real_fdopen(descriptor, mode, *args, **kwargs)

    with mock.patch.object(file_path_policy.os, "open", side_effect=capture), \
            mock.patch("os.fdopen", side_effect=construct), \
            mock.patch("subprocess.run") as installer:
        result = service.checksum(str(path), "sha256") if operation == "checksum" \
            else service.install(str(path))
        assert result[0] == errno.EIO
    installer.assert_not_called()
    assert list(public_root.glob(".sonic-image-install-*")) == []
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
