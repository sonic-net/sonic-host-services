import errno
import array
import hashlib
import json
import os
import stat
import struct
import socket
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from host_modules import file_path_policy, image_file_worker, image_service, image_store
from host_modules.image_store import DENIED, EXPORT_ROOT, ImageStore, artifact_parts


@pytest.fixture
def service():
    with mock.patch("dbus.SystemBus"), mock.patch("dbus.service.BusName"), \
            mock.patch("dbus.service.Object.__init__"):
        yield image_service.ImageService(mod_name="image_service")


@pytest.mark.parametrize("path", [
    "/etc/shadow", "/etc/shadow.missing", "/root/missing", "/proc/self/root/etc/shadow",
    "/tmp/private.bin", "/home/admin/.ssh/id_ed25519", "/host/config_db.json",
    "/host/image-test/rw/etc/shadow", "//etc/shadow", "/tmp/../etc/shadow",
    EXPORT_ROOT + "-other/image.bin", EXPORT_ROOT + "/../staging/private",
    "file:///etc/shadow",
])
def test_unexported_checksum_denied_without_probe(service, path):
    with mock.patch("os.path.isfile", side_effect=AssertionError("existence probe")), \
            mock.patch("host_modules.image_store.ImageStore.credentials",
                       side_effect=AssertionError("account lookup")):
        assert service.checksum(path, "sha256") == (
            errno.EACCES, DENIED
        )
    with mock.patch("subprocess.run", side_effect=AssertionError("privileged installer")):
        assert service.install(path) == (errno.EACCES, DENIED)


@pytest.mark.parametrize("path", ["relative.bin", EXPORT_ROOT + "/.hidden",
                                 EXPORT_ROOT + "/a/../../image.bin"])
def test_invalid_artifact_names(path):
    with pytest.raises(PermissionError):
        artifact_parts(path)


@pytest.fixture
def store(monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("real credential-drop tests require an isolated root test container")
    with tempfile.TemporaryDirectory(prefix="image-export-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o755)
        (root / "host").mkdir(mode=0o755)
        (root / "etc").mkdir(mode=0o755)
        (root / "etc/passwd").write_text(
            "sonic-image:x:65534:65534:Image worker:/nonexistent:/usr/sbin/nologin\n"
        )
        result = ImageStore(str(root))
        result.prepare()
        monkeypatch.setattr(file_path_policy, "_store", result)
        monkeypatch.setattr(image_service, "ImageStore", lambda: result)
        monkeypatch.setattr(image_service, "INSTALL_STAGING_PARENT", str(root / "host"))
        yield result


def test_private_download_checksum_real_unprivileged_worker(service, store):
    path = EXPORT_ROOT + "/sonic.bin"
    response = mock.Mock(status_code=200)
    response.iter_content.return_value = iter([b"synthetic image"])
    with mock.patch("requests.get", return_value=response):
        assert service.download("https://example.com/image.bin", path) == (
            0, "Download successful"
        )
    physical = Path(store._physical(path))
    info = physical.stat()
    assert info.st_uid == 65534 and info.st_gid == 65534
    assert info.st_mode & 0o777 == 0o600
    assert service.checksum(path, "sha256") == (
        0, hashlib.sha256(b"synthetic image").hexdigest()
    )
    assert list(Path(store._physical("/host/sonic-image-exports/staging")).iterdir()) == []


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_even_root_caller_cannot_read_root_owned_export(service, store, mode):
    path = EXPORT_ROOT + "/root-file.bin"
    target = Path(store._physical(path))
    target.write_bytes(b"synthetic root-only secret")
    target.chmod(mode)
    assert service.checksum(path, "sha256") == (
        errno.EACCES, DENIED
    )
    assert service.install(path) == (errno.EACCES, DENIED)


def test_missing_and_inaccessible_exports_have_same_response(service, store):
    existing = EXPORT_ROOT + "/root-file.bin"
    Path(store._physical(existing)).write_bytes(b"synthetic secret")
    assert service.checksum(existing, "sha256") == service.checksum(
        EXPORT_ROOT + "/missing.bin", "sha256"
    )


def test_aliases_are_not_exported(service, store):
    path = EXPORT_ROOT + "/image.bin"
    with store.create(path) as output:
        output.write(b"image")
    original = Path(store._physical(path))
    symbolic = Path(store._physical(EXPORT_ROOT + "/symbolic.bin"))
    symbolic.symlink_to(original)
    hardlink = Path(store._physical(EXPORT_ROOT + "/hardlink.bin"))
    os.link(original, hardlink)
    for name in ("image.bin", "symbolic.bin", "hardlink.bin"):
        assert service.checksum(EXPORT_ROOT + "/" + name, "sha256")[0] == errno.EACCES


def test_private_parent_is_not_bypassed_by_root(service, store):
    parent = Path(store._physical(EXPORT_ROOT + "/root-private"))
    parent.mkdir(mode=0o700)
    target = parent / "image.bin"
    target.write_bytes(b"image")
    target.chmod(0o600)
    os.chown(target, 65534, 65534)
    assert service.checksum(EXPORT_ROOT + "/root-private/image.bin", "sha256")[0] == errno.EACCES


def test_failed_upload_is_not_published(store):
    path = EXPORT_ROOT + "/failed.bin"
    with pytest.raises(RuntimeError):
        with store.create(path) as output:
            output.write(b"partial")
            raise RuntimeError("synthetic transfer failure")
    assert not Path(store._physical(path)).exists()
    assert list(Path(store._physical("/host/sonic-image-exports/staging")).iterdir()) == []


def test_abort_does_not_require_images_directory(store):
    with pytest.raises(RuntimeError):
        with store.create(EXPORT_ROOT + "/aborted.bin") as output:
            output.write(b"partial")
            Path(store._physical(EXPORT_ROOT)).rmdir()
            raise RuntimeError("synthetic transfer failure")
    assert list(Path(store._physical("/host/sonic-image-exports/staging")).iterdir()) == []


def test_identity_upgrade_rollback_and_interrupted_migration(store):
    path = EXPORT_ROOT + "/release/sonic.bin"
    with store.create(path) as output:
        output.write(b"image survives identity changes")
    passwd = Path(store._physical("/etc/passwd"))
    real_chown = os.fchown
    interrupted = False

    def interrupt(descriptor, uid, gid):
        nonlocal interrupted
        real_chown(descriptor, uid, gid)
        if not interrupted:
            interrupted = True
            raise OSError(errno.EINTR, "synthetic power loss")

    passwd.write_text("sonic-image:x:65533:65533:Image:/nonexistent:/usr/sbin/nologin\n")
    with mock.patch("os.fchown", side_effect=interrupt):
        with pytest.raises(OSError):
            store.prepare()
    for uid in (65533, 65534, 65532, 65534):
        passwd.write_text(f"sonic-image:x:{uid}:{uid}:Image:/nonexistent:/usr/sbin/nologin\n")
        with store.open(path) as source:
            assert source.read() == b"image survives identity changes"
        for logical in (EXPORT_ROOT, EXPORT_ROOT + "/release", path,
                        "/host/sonic-image-exports/staging"):
            info = Path(store._physical(logical)).stat()
            assert info.st_uid == uid and info.st_gid == uid
            assert stat.S_IMODE(info.st_mode) == (0o600 if logical == path else 0o700)
        record = json.loads(Path(store._physical(
            "/host/sonic-image-exports/ownership.json")).read_text())
        assert record["uid"] == uid and record["next_uid"] == 0


@pytest.mark.parametrize("kind", ["root-owner", "unknown-owner", "public-mode",
                                 "symlink", "hardlink", "fifo", "hidden",
                                 "staging-directory"])
def test_migration_rejects_unsafe_tree_before_changing_valid_artifact(store, kind):
    path = EXPORT_ROOT + "/safe.bin"
    with store.create(path) as output:
        output.write(b"safe")
    safe = Path(store._physical(path))
    unsafe = Path(store._physical(EXPORT_ROOT + "/unsafe.bin"))
    if kind == "symlink":
        unsafe.symlink_to(safe)
    elif kind == "hardlink":
        os.link(safe, unsafe)
    elif kind == "fifo":
        os.mkfifo(unsafe, 0o600)
        os.chown(unsafe, 65534, 65534)
    elif kind == "staging-directory":
        unsafe = Path(store._physical(
            "/host/sonic-image-exports/staging/0123456789abcdef0123456789abcdef"))
        unsafe.mkdir(mode=0o700)
        os.chown(unsafe, 65534, 65534)
    else:
        if kind == "hidden":
            unsafe = unsafe.with_name(".hidden")
        unsafe.write_bytes(b"unsafe")
        uid = {"root-owner": 0, "unknown-owner": 65532}.get(kind, 65534)
        os.chown(unsafe, uid, uid)
        unsafe.chmod(0o644 if kind == "public-mode" else 0o600)
    Path(store._physical("/etc/passwd")).write_text(
        "sonic-image:x:65533:65533:Image:/nonexistent:/usr/sbin/nologin\n")
    with pytest.raises(OSError):
        store.prepare()
    assert safe.stat().st_uid == 65534


@pytest.mark.parametrize("data", ["{}", "{",
    '{"version":1,"uid":0,"gid":0,"next_uid":0,"next_gid":0}',
    '{"version":1,"uid":65534,"gid":65534,"next_uid":65533,"next_gid":0}'])
def test_malformed_ownership_journal_fails_closed(store, data):
    Path(store._physical("/host/sonic-image-exports/ownership.json")).write_text(data)
    with pytest.raises(OSError):
        store.prepare()


def test_nested_private_images(store):
    path = EXPORT_ROOT + "/release/sonic.bin"
    with store.create(path) as output:
        output.write(b"image")
    with store.open(path) as source:
        assert not os.get_inheritable(source.fileno())
        assert source.read() == b"image"


def test_missing_or_root_service_identity_fails_closed(store):
    passwd = Path(store._physical("/etc/passwd"))
    for record in ("", "sonic-image:x:0:0:Invalid:/root:/usr/sbin/nologin\n"):
        passwd.write_text(record)
        with pytest.raises(OSError):
            store.credentials()


def test_existing_unsafe_store_is_not_repermissioned(store):
    images = Path(store._physical(EXPORT_ROOT))
    images.chmod(0o755)
    with pytest.raises(PermissionError):
        store.prepare()
    assert images.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("returncode", [0, 1])
def test_local_install_uses_private_copy_and_cleans_up(service, store, returncode):
    path = EXPORT_ROOT + "/sonic.bin"
    with store.create(path) as image:
        image.write(b"synthetic image")
    installed = []

    def install(command, **kwargs):
        copy = Path(command[-1])
        installed.append(copy)
        assert str(copy) != store._physical(path)
        assert copy.read_bytes() == b"synthetic image"
        assert copy.stat().st_uid == 0
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
        return mock.Mock(returncode=returncode, stderr=b"Error: synthetic failure")

    with mock.patch("subprocess.run", side_effect=install):
        assert service.install(path)[0] == returncode
    assert len(installed) == 1 and not installed[0].parent.exists()
    with store.open(path) as source:
        assert source.read() == b"synthetic image"


def test_local_install_copy_failure_cleans_up(service, store):
    path = EXPORT_ROOT + "/sonic.bin"
    with store.create(path) as image:
        image.write(b"image")
    with mock.patch("shutil.copyfileobj", side_effect=OSError("synthetic disk full")), \
            mock.patch("subprocess.run") as installer:
        assert service.install(path) == (errno.EIO, "Image installation failed")
    installer.assert_not_called()
    assert list(Path(store._physical("/host")).glob(".sonic-image-install-*")) == []


def test_posix_acl_enforced_by_actual_unprivileged_worker(service, store):
    path = EXPORT_ROOT + "/sonic.bin"
    with store.create(path) as image:
        image.write(b"image")
    # A named-user deny on a root-owned ancestor is invisible to mode-bit-only
    # checks. The actual non-root open must enforce it.
    entries = [(0x01, 7, 0xFFFFFFFF), (0x02, 0, 65534),
               (0x04, 5, 0xFFFFFFFF), (0x10, 5, 0xFFFFFFFF),
               (0x20, 5, 0xFFFFFFFF)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(store._physical("/host"), "system.posix_acl_access", acl)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            pytest.skip("fixture filesystem does not support POSIX ACLs")
        raise
    assert service.checksum(path, "sha256") == (errno.EACCES, DENIED)


def test_worker_dispatch_and_publication_contract(store):
    path = EXPORT_ROOT + "/image.bin"
    # Isolate dispatch/ownership assertions from credential mechanics. The
    # separate real-worker tests above enforce actual kernel permissions.
    with mock.patch("os.geteuid", return_value=65534), \
            mock.patch("os.getegid", return_value=65534), \
            mock.patch("os.getuid", return_value=65534), \
            mock.patch("os.getgroups", return_value=[]):
        descriptor, token = image_store.worker_operation(store.host_root, "create", path, "")
        try:
            os.fchown(descriptor, 65534, 65534)
            os.write(descriptor, b"unit image")
        finally:
            os.close(descriptor)
        image_store.worker_operation(store.host_root, "publish", path, token)
        descriptor, _ = image_store.worker_operation(store.host_root, "open", path, "")
        with os.fdopen(descriptor, "rb") as source:
            assert source.read() == b"unit image"
        descriptor, _ = image_store.worker_operation(store.host_root, "open", EXPORT_ROOT, "")
        os.close(descriptor)
        descriptor, token = image_store.worker_operation(store.host_root, "create", path, "")
        os.close(descriptor)
        image_store.worker_operation(store.host_root, "abort", path, token)
        image_store.worker_operation(store.host_root, "abort", path, token)
        for operation, token in (("invalid", ""), ("publish", "invalid"), ("abort", "../bad")):
            with pytest.raises(PermissionError):
                image_store.worker_operation(store.host_root, operation, path, token)


@pytest.mark.parametrize("error_number", [
    errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENXIO, errno.EIO,
])
def test_worker_protocol_uniform_denial_and_explicit_failure(error_number):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, mock.patch("sys.argv", ["worker", str(child.detach()), "/", "open",
                                       EXPORT_ROOT + "/image.bin", ""]), \
            mock.patch.object(image_file_worker, "worker_operation",
                              side_effect=OSError(error_number, "synthetic failure")):
        assert image_file_worker.main() == 1
        assert parent.recv(128) == (b"ERROR" if error_number == errno.EIO else b"DENIED")


def test_worker_protocol_transfers_descriptor_without_reopening(tmp_path):
    image = tmp_path / "authorized.bin"
    image.write_bytes(b"image")
    descriptor = os.open(image, os.O_RDONLY)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with parent, mock.patch("sys.argv", ["worker", str(child.detach()), "/", "open",
                                       EXPORT_ROOT + "/image.bin", ""]), \
            mock.patch.object(image_file_worker, "worker_operation", return_value=(descriptor, "")):
        assert image_file_worker.main() == 0
        payload, ancillary, _, _ = parent.recvmsg(128, socket.CMSG_SPACE(4),
                                                socket.MSG_CMSG_CLOEXEC)
        assert payload == b"OK "
        received = array.array("i")
        received.frombytes(ancillary[0][2])
        assert len(received) == 1
        with os.fdopen(received[0], "rb") as source:
            assert source.read() == b"image"
            assert not os.get_inheritable(source.fileno())
