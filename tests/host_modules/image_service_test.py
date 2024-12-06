import hashlib
import subprocess
import sys
import os
import stat
import pytest
from unittest import mock
from host_modules.image_service import ImageService


class TestImageService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isdir")
    @mock.patch("os.stat")
    @mock.patch("requests.get")
    def test_download_success(
        self, mock_get, mock_stat, mock_isdir, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `download` method successfully downloads an image when the directory exists and is writable.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/sonic_image.img"
        save_as = "/tmp/sonic_image.img"
        mock_isdir.return_value = True
        mock_stat.return_value.st_mode = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        mock_response = mock.Mock()
        mock_response.iter_content = lambda chunk_size: [b"data"]
        mock_response.status_code = 200
        mock_response.iter_content = lambda chunk_size: iter([b"data"])
        mock_get.return_value = mock_response

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc == 0, "wrong return value"
        assert (
            "download" in msg.lower() and "successful" in msg.lower()
        ), "message should contains 'download' and 'successful'"
        mock_get.assert_called_once_with(image_url, stream=True)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isdir")
    def test_download_fail_no_dir(
        self, mock_isdir, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `download` method fails when the directory does not exist.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/sonic_image.img"
        save_as = "/nonexistent_dir/sonic_image.img"
        mock_isdir.return_value = False

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "not" in msg.lower() and "exist" in msg.lower()
        ), "message should contains 'not' and 'exist'"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isdir")
    @mock.patch("os.stat")
    def test_download_fail_missing_other_write(
        self, mock_stat, mock_isdir, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `download` method fails when the directory is not writable by others.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/sonic_image.img"
        save_as = "/tmp/sonic_image.img"
        mock_isdir.return_value = True
        mock_stat.return_value.st_mode = (
            stat.S_IWUSR | stat.S_IWGRP
        )  # Missing write permission for others

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "permission" in msg.lower() or "writable" in msg.lower()
        ), "message should contain 'permission' or 'writable'"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_download_failed_relative_path(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download` method fails when the save_as path is not absolute.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/sonic_image.img"
        save_as = "relative/path/sonic_image.img"

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc != 0, "wrong return value"
        assert "absolute" in msg.lower(), "message should contain 'absolute'"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isdir")
    @mock.patch("os.stat")
    @mock.patch("requests.get")
    def test_download_failed_not_found(
        self, mock_get, mock_stat, mock_isdir, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `download` method fails when the image URL is not found (404 error).
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/nonexistent_image.img"
        save_as = "/tmp/sonic_image.img"
        mock_isdir.return_value = True
        mock_stat.return_value.st_mode = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        mock_response = mock.Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "404" in msg and "error" in msg.lower()
        ), "message should contain '404' and 'error'"
        mock_get.assert_called_once_with(image_url, stream=True)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isdir")
    @mock.patch("os.stat")
    @mock.patch("requests.get")
    @mock.patch("tempfile.NamedTemporaryFile")
    def test_download_fail_write_io_exception(
        self,
        mock_tempfile,
        mock_get,
        mock_stat,
        mock_isdir,
        MockInit,
        MockBusName,
        MockSystemBus,
    ):
        """
        Test that the `download` method fails when there is an IOError while writing the file.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image_url = "http://example.com/sonic_image.img"
        save_as = "/tmp/sonic_image.img"
        mock_isdir.return_value = True
        mock_stat.return_value.st_mode = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        mock_response = mock.Mock()
        mock_response.iter_content = lambda chunk_size: [b"data"]
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        mock_tempfile.side_effect = IOError("Disk write error")

        # Act
        rc, msg = image_service.download(image_url, save_as)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "disk write error" in msg.lower()
        ), "message should contain 'disk write error'"
        mock_get.assert_called_once_with(image_url, stream=True)
        mock_tempfile.assert_called_once_with(
            delete=False, dir=os.path.dirname(save_as)
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.run")
    def test_install_success(self, mock_run, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `install` method successfully installs an image.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        where = "/tmp/sonic_image.img"
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        mock_run.return_value = mock_result

        # Act
        rc, msg = image_service.install(where)

        # Assert
        assert rc == 0, "wrong return value"
        assert msg == "", "message should be empty on success"
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "install", "-y", where],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.run")
    def test_install_fail(self, mock_run, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `install` method fails when the installation command returns a non-zero exit code.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        where = "/tmp/sonic_image.img"
        mock_result = mock.Mock()
        mock_result.returncode = 1
        mock_result.stderr = b"Error: Installation failed"
        mock_run.return_value = mock_result

        # Act
        rc, msg = image_service.install(where)

        # Assert
        assert rc != 0, "wrong return value"
        assert "Error" in msg, "message should contain 'Error'"
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "install", "-y", where],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @pytest.mark.parametrize(
        "algorithm, expected_checksum",
        [
            ("sha256", hashlib.sha256(b"test data").hexdigest()),
            ("sha512", hashlib.sha512(b"test data").hexdigest()),
            ("md5", hashlib.md5(b"test data").hexdigest()),
        ],
    )
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isfile")
    @mock.patch("builtins.open", new_callable=mock.mock_open, read_data=b"test data")
    def test_checksum(
        self,
        mock_open,
        mock_isfile,
        MockInit,
        MockBusName,
        MockSystemBus,
        algorithm,
        expected_checksum,
    ):
        """
        Test that the `checksum` method correctly calculates the checksum of a file for different algorithms.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/tmp/test_file.img"
        mock_isfile.return_value = True

        # Act
        rc, checksum = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc == 0, "wrong return value"
        assert checksum == expected_checksum, "checksum does not match expected value"
        mock_isfile.assert_called_once_with(file_path)
        mock_open.assert_called_once_with(file_path, "rb")

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isfile")
    def test_checksum_no_such_file(
        self, mock_isfile, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method fails when the file does not exist.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/nonexistent_dir/test_file.img"
        algorithm = "sha256"
        mock_isfile.return_value = False

        # Act
        rc, msg = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc != 0, "wrong return value"
        assert "not exist" in msg.lower(), "message should contain 'not exist'"
        mock_isfile.assert_called_once_with(file_path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isfile")
    def test_checksum_unsupported_algorithm(
        self, mock_isfile, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method fails when an unsupported algorithm is provided.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/tmp/test_file.img"
        algorithm = "unsupported_algo"
        mock_isfile.return_value = True

        # Act
        rc, msg = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "unsupported algorithm" in msg.lower()
        ), "message should contain 'unsupported algorithm'"
        mock_isfile.assert_called_once_with(file_path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.path.isfile")
    @mock.patch("builtins.open", new_callable=mock.mock_open, read_data=b"test data")
    def test_checksum_general_exception(
        self, mock_open, mock_isfile, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method handles general exceptions during file reading.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/tmp/test_file.img"
        algorithm = "sha256"
        mock_isfile.return_value = True

        with mock.patch.object(hashlib, algorithm) as mock_hash_func:
            mock_hash_instance = mock_hash_func.return_value
            mock_hash_instance.update.side_effect = Exception("General error")

            # Act
            rc, msg = image_service.checksum(file_path, algorithm)

            # Assert
            assert rc != 0, "wrong return value"
            assert (
                "general error" in msg.lower()
            ), "message should contain 'general error'"
            mock_isfile.assert_called_once_with(file_path)
            mock_open.assert_called_once_with(file_path, "rb")
