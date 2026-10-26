import errno
import hashlib
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from host_modules import file_path_policy
from host_modules.image_service import ImageService


@pytest.fixture
def image_service():
    with mock.patch("dbus.SystemBus"), mock.patch("dbus.service.BusName"), \
            mock.patch("dbus.service.Object.__init__"):
        yield ImageService(mod_name="image_service")


@pytest.mark.parametrize("path", [
    "/etc/shadow", "//etc/shadow", "/tmp/../etc/shadow",
    "/etc/gshadow-", "/etc/sudoers.d/missing",
    "/etc/shadow.bak", "/etc/gshadow.old", "/etc/sudoers.dpkg-old",
    "/host/image-SONiC-test/rw/etc/shadow",
    "/host/image-SONiC-test/rw/root/secret",
    "/root/missing", "/proc/self/root/etc/shadow",
    "/dev/fd/0", "/run/missing",
    "/etc/sonic/config_db.json", "/etc/ssl/private/missing",
    "/home/admin/.ssh/id_ed25519", "/home/admin/.gnupg/missing",
    "/host/config_db.json.before_test_module", "/host/minigraph.xml",
    "/host/glome/glome.conf", "/etc/ssh/ssh_host_rsa_key",
])
def test_sensitive_checksum_denied_before_existence_probe(image_service, path):
    with mock.patch("os.path.isfile", side_effect=AssertionError("existence probe")):
        assert image_service.checksum(path, "sha256") == (
            errno.EACCES, "Sensitive pathname access denied"
        )


@pytest.fixture
def protected_path(tmp_path, monkeypatch):
    protected = tmp_path / "protected"
    protected.mkdir()
    monkeypatch.setattr(
        file_path_policy, "BLOCKED_DIRECTORIES",
        file_path_policy.BLOCKED_DIRECTORIES + (str(protected),),
    )
    return protected


@pytest.mark.parametrize("exists", [False, True])
def test_sensitive_symlink_denial_does_not_reveal_existence(
        image_service, tmp_path, protected_path, exists):
    target = protected_path / "secret"
    if exists:
        target.write_bytes(b"synthetic secret")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    assert image_service.checksum(str(alias), "sha256") == (
        errno.EACCES, "Sensitive pathname access denied"
    )


def test_sensitive_directory_alias(image_service, tmp_path, protected_path):
    (protected_path / "secret").write_bytes(b"synthetic secret")
    alias = tmp_path / "alias"
    alias.symlink_to(protected_path, target_is_directory=True)
    assert image_service.checksum(str(alias / "secret"), "sha256")[0] == errno.EACCES


def test_sensitive_hardlink_alias(image_service, tmp_path, protected_path):
    secret = protected_path / "secret"
    secret.write_bytes(b"synthetic secret")
    alias = tmp_path / "alias"
    os.link(secret, alias)
    assert image_service.checksum(str(alias), "sha256")[0] == errno.EACCES


def test_directory_swap_cannot_bypass_checksum_policy(
        image_service, tmp_path, protected_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "image.bin").write_bytes(b"public image")
    (protected_path / "image.bin").write_bytes(b"synthetic secret")
    real_open = os.open

    def swap_then_open(path, flags):
        staging.rename(tmp_path / "old-staging")
        staging.symlink_to(protected_path, target_is_directory=True)
        return real_open(path, flags)

    monkeypatch.setattr(file_path_policy.os, "open", swap_then_open)
    assert image_service.checksum(str(staging / "image.bin"), "sha256")[0] == errno.EACCES


def test_private_image_and_safe_symlink_remain_supported(image_service, tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"image contents")
    image.chmod(0o600)
    alias = tmp_path / "image-link.bin"
    alias.symlink_to(image)
    expected = (0, hashlib.sha256(b"image contents").hexdigest())
    assert image_service.checksum(str(image), "sha256") == expected
    assert image_service.checksum(str(alias), "sha256") == expected


def test_final_symlink_swap_denied(
        image_service, tmp_path, protected_path, monkeypatch):
    image = tmp_path / "image.bin"
    image.write_bytes(b"image")
    real_open = os.open

    def swap_then_open(path, flags):
        image.unlink()
        image.symlink_to(protected_path / "secret")
        return real_open(path, flags)

    monkeypatch.setattr(file_path_policy.os, "open", swap_then_open)
    assert image_service.checksum(str(image), "sha256") == (
        errno.EACCES, "Sensitive pathname access denied"
    )


def test_download_then_checksum_preserves_private_image(image_service):
    # download() stages in /tmp and requires an atomic, same-filesystem rename.
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        destination = Path(directory) / "downloads"
        destination.mkdir(mode=0o777)
        destination.chmod(0o777)
        image = destination / "image.bin"
        response = mock.Mock(status_code=200)
        response.iter_content.return_value = iter([b"downloaded image"])
        with mock.patch("requests.get", return_value=response):
            assert image_service.download("https://example.com/image.bin", str(image))[0] == 0
        assert image.stat().st_mode & 0o777 == 0o600
        assert image_service.checksum(str(image), "sha256") == (
            0, hashlib.sha256(b"downloaded image").hexdigest()
        )


@pytest.mark.parametrize("path", [
    "/rootfs/image.bin", "/etc/shadow-image.bin",
    "/etc/ssh/ssh_host_rsa_key.pub", "/host/image.bin",
    "/host/image-SONiC-test/rw/tmp/image.bin",
])
def test_blocklist_component_boundaries(path):
    assert not file_path_policy.is_sensitive_path(path)
