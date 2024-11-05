import sys
import os
import pytest
from unittest import mock
from host_modules.image_service import ImageService


class TestImageService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_download_success(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download_sonic_image` method runs the correct curl command
        when the directory path exists.
        """
        with (
            mock.patch("os.path.exists", return_value=True) as mock_exists,
            mock.patch("subprocess.run") as mock_run,
        ):
            # Arrange: Set up an Installer instance and define the target path and URL
            image_service = ImageService(mod_name="image_service")
            target_path = "/path/to/sonic_image.img"
            image_url = "https://example.com/sonic_image.img"
            run_ret = mock.Mock()
            attrs = {"returncode": 0, "stderr": b""}
            run_ret.configure_mock(**attrs)
            mock_run.return_value = run_ret

            # Act: Call the method to download the image
            rc, msg = image_service.download(image_url, target_path)

            # Assert: Verify that os.path.exists was called to check directory existence
            mock_exists.assert_called_once_with(os.path.dirname(target_path))
            assert rc == 0, "wrong return value"
            assert msg == "", "non-empty return message"

            # Assert: Verify that subprocess.run was called with the correct curl command
            mock_run.assert_called_once_with(
                ["/usr/bin/curl", "-Lo", target_path, image_url],
                stdout=mock.ANY,
                stderr=mock.ANY,
            )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_download_mkdir(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download_sonic_image` method runs the correct curl command
        when the directory path exists.
        """
        with (
            mock.patch("os.path.exists", return_value=False) as mock_exists,
            mock.patch("os.makedirs") as mock_mkdirs,
            mock.patch("subprocess.run", return_value=0) as mock_run,
        ):
            # Arrange: Set up an Installer instance and define the target path and URL
            image_service = ImageService(mod_name="image_service")
            target_path = "/path/to/sonic_image.img"
            image_url = "https://example.com/sonic_image.img"
            run_ret = mock.Mock()
            attrs = {"returncode": 0, "stderr": b""}
            run_ret.configure_mock(**attrs)
            mock_run.return_value = run_ret

            # Act: Call the method to download the image
            rc, msg = image_service.download(image_url, target_path)
            assert rc == 0, "wrong return value"
            assert msg == "", "non-empty return message"

            # Assert: Verify that os.path.exists was called to check directory existence
            mock_exists.assert_called_once_with(os.path.dirname(target_path))
            mock_mkdirs.assert_called_once_with(os.path.dirname(target_path))
            # Assert: Verify that subprocess.run was called with the correct curl command
            mock_run.assert_called_once_with(
                ["/usr/bin/curl", "-Lo", target_path, image_url],
                stdout=mock.ANY,
                stderr=mock.ANY,
            )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_download_download_fail(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download_sonic_image` method runs the correct curl command
        when the directory path exists.
        """
        with (
            mock.patch("os.path.exists", return_value=False) as mock_exists,
            mock.patch("os.makedirs") as mock_mkdirs,
            mock.patch("subprocess.run", return_value=0) as mock_run,
        ):
            # Arrange: Set up an Installer instance and define the target path and URL
            image_service = ImageService(mod_name="image_service")
            target_path = "/path/to/sonic_image.img"
            image_url = "https://example.com/sonic_image.img"
            run_ret = mock.Mock()
            # Download failed.
            attrs = {"returncode": 1, "stderr": b"Error: Download failed\nHello World!"}
            run_ret.configure_mock(**attrs)
            mock_run.return_value = run_ret

            # Act: Call the method to download the image
            rc, msg = image_service.download(image_url, target_path)
            assert rc != 0, "wrong return value"
            assert "Error" in msg, "return message without error"

            # Assert: Verify that os.path.exists was called to check directory existence
            mock_exists.assert_called_once_with(os.path.dirname(target_path))
            mock_mkdirs.assert_called_once_with(os.path.dirname(target_path))
            # Assert: Verify that subprocess.run was called with the correct curl command
            mock_run.assert_called_once_with(
                ["/usr/bin/curl", "-Lo", target_path, image_url],
                stdout=mock.ANY,
                stderr=mock.ANY,
            )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_install_failed(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download_sonic_image` method runs the correct curl command
        when the directory path exists.
        """
        with mock.patch("subprocess.run") as mock_run:
            # Arrange: Set up an Installer instance and define the target path and URL
            image_service = ImageService(mod_name="image_service")
            target_path = "/path/to/sonic_image.img"
            run_ret = mock.Mock()
            attrs = {"returncode": 1, "stderr": b"Error: Install failed\nHello World!"}
            run_ret.configure_mock(**attrs)
            mock_run.return_value = run_ret

            # Act: Call the method to download the image
            rc, msg = image_service.install(target_path)

            # Assert: Verify that os.path.exists was called to check directory existence
            assert rc != 0, "wrong return value"
            assert "Error" in msg, "wrong return message"

            # Assert: Verify that subprocess.run was called with the correct curl command
            mock_run.assert_called_once_with(
                [
                    "sudo",
                    "/usr/local/bin/sonic-installer",
                    "install",
                    "-y",
                    target_path,
                ],
                stdout=mock.ANY,
                stderr=mock.ANY,
            )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_install_success(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `download_sonic_image` method runs the correct curl command
        when the directory path exists.
        """
        with mock.patch("subprocess.run") as mock_run:
            # Arrange: Set up an Installer instance and define the target path and URL
            image_service = ImageService(mod_name="image_service")
            target_path = "/path/to/sonic_image.img"
            run_ret = mock.Mock()
            attrs = {"returncode": 0, "stderr": b""}
            run_ret.configure_mock(**attrs)
            mock_run.return_value = run_ret

            # Act: Call the method to download the image
            rc, msg = image_service.install(target_path)

            # Assert: Verify that os.path.exists was called to check directory existence
            assert rc == 0, "wrong return value"
            assert msg == "", "non-empty return message"

            # Assert: Verify that subprocess.run was called with the correct curl command
            mock_run.assert_called_once_with(
                [
                    "sudo",
                    "/usr/local/bin/sonic-installer",
                    "install",
                    "-y",
                    target_path,
                ],
                stdout=mock.ANY,
                stderr=mock.ANY,
            )
