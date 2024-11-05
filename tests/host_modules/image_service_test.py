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
