import hashlib
import subprocess
import pytest
import json
import errno
from unittest import mock
from host_modules.image_service import ImageService


class TestImageService(object):
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
    @mock.patch("host_modules.image_service.open_checksum_file",
                new_callable=mock.mock_open, read_data=b"test data")
    def test_checksum(
        self,
        mock_open,
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

        # Act
        rc, checksum = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc == 0, "wrong return value"
        assert checksum == expected_checksum, "checksum does not match expected value"
        mock_open.assert_called_once_with(file_path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.image_service.open_checksum_file",
                side_effect=PermissionError(errno.EACCES, "File read access denied"))
    def test_checksum_no_such_file(
        self, mock_open, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method fails when the file does not exist.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/nonexistent_dir/test_file.img"
        algorithm = "sha256"

        # Act
        rc, msg = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc != 0, "wrong return value"
        assert (rc, msg) == (errno.EACCES, "File read access denied")
        mock_open.assert_called_once_with(file_path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.image_service.open_checksum_file")
    def test_checksum_unsupported_algorithm(
        self, mock_open, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method fails when an unsupported algorithm is provided.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/tmp/test_file.img"
        algorithm = "unsupported_algo"

        # Act
        rc, msg = image_service.checksum(file_path, algorithm)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "unsupported algorithm" in msg.lower()
        ), "message should contain 'unsupported algorithm'"
        mock_open.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.image_service.open_checksum_file",
                new_callable=mock.mock_open, read_data=b"test data")
    def test_checksum_general_exception(
        self, mock_open, MockInit, MockBusName, MockSystemBus
    ):
        """
        Test that the `checksum` method handles general exceptions during file reading.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        file_path = "/tmp/test_file.img"
        algorithm = "sha256"

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
            mock_open.assert_called_once_with(file_path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_success(self, mock_check_output, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `list_images` method successfully lists the current, next, and available SONiC images.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_output = (
            "Current: current_image\n"
            "Next: next_image\n"
            "Available:\n"
            "image1\n"
            "image2\n"
        )
        mock_check_output.return_value = mock_output.encode()

        # Act
        rc, images_json = image_service.list_images()
        images = json.loads(images_json)

        # Assert
        assert rc == 0, "wrong return value"
        assert images["current"] == "current_image", "current image does not match"
        assert images["next"] == "next_image", "next image does not match"
        assert images["available"] == ["image1", "image2"], "available images do not match"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_success_lowercase_output(self, mock_check_output, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `list_images` method successfully lists the current, next, and available SONiC images
        even if the output from sonic-installer is in lowercase.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_output = (
            "current: current_image\n"
            "next: next_image\n"
            "available:\n"
            "image1\n"
            "image2\n"
        )
        mock_check_output.return_value = mock_output.encode()

        # Act
        rc, images_json = image_service.list_images()
        images = json.loads(images_json)

        # Assert
        assert rc == 0, "wrong return value"
        assert images["current"] == "current_image", "current image does not match"
        assert images["next"] == "next_image", "next image does not match"
        assert images["available"] == ["image1", "image2"], "available images do not match"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )

    @pytest.mark.parametrize(
        "mock_output, expected_current, expected_next, expected_available",
        [
            ("Current: \nNext: next_image\nAvailable:\nimage1\nimage2\n", "", "next_image", ["image1", "image2"]),
            ("Current: current_image\nNext: \nAvailable:\nimage1\nimage2\n", "current_image", "", ["image1", "image2"]),
            ("Current: current_image\nNext: next_image\nAvailable:\n", "current_image", "next_image", []),
        ],
    )
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_success_empty_image_output(
        self, mock_check_output, MockInit, MockBusName, MockSystemBus, mock_output, expected_current, expected_next, expected_available
    ):
        """
        Test that the `list_images` method successfully lists the current, next, and available SONiC images even if
        sonic-installer output empty string for the image name.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_check_output.return_value = mock_output.encode()

        # Act
        rc, images_json = image_service.list_images()
        images = json.loads(images_json)

        # Assert
        assert rc == 0, "wrong return value"
        assert images["current"] == expected_current, "current image does not match"
        assert images["next"] == expected_next, "next image does not match"
        assert images["available"] == expected_available, "available images do not match"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )

    @pytest.mark.parametrize(
        "mock_output, expected_current, expected_next, expected_available",
        [
            ("Next: next_image\nAvailable:\nimage1\nimage2\n", "", "next_image", ["image1", "image2"]),
            ("Current: current_image\nAvailable:\nimage1\nimage2\n", "current_image", "", ["image1", "image2"]),
            ("Current: current_image\nNext: next_image\n", "current_image", "next_image", []),
            ("Available:\nimage1\nimage2\n", "", "", ["image1", "image2"]),
            ("Current: current_image\nNext: next_image\nAvailable:\n", "current_image", "next_image", []),
        ],
    )
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_various_missing_lines(
        self, mock_check_output, MockInit, MockBusName, MockSystemBus, mock_output, expected_current, expected_next, expected_available
    ):
        """
        Test that the `list_images` method handles various scenarios where the sonic-installer output is missing lines for current, next, or available images.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_check_output.return_value = mock_output.encode()

        # Act
        rc, images_json = image_service.list_images()
        images = json.loads(images_json)

        # Assert
        assert rc == 0, "wrong return value"
        assert images["current"] == expected_current, "current image does not match"
        assert images["next"] == expected_next, "next image does not match"
        assert images["available"] == expected_available, "available images do not match"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_success_empty_available_images(self, mock_check_output, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `list_images` method successfully lists the current, next, and available SONiC images.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_output = (
            "Current: current_image\n"
            "Next: next_image\n"
            "Available:\n"
        )
        mock_check_output.return_value = mock_output.encode()

        # Act
        rc, images_json = image_service.list_images()
        images = json.loads(images_json)

        # Assert
        assert rc == 0, "wrong return value"
        assert images["available"] == [], "available images should be empty"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )


    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.check_output")
    def test_list_images_failed(self, mock_check_output, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `list_image` method fails when the subprocess command returns a non-zero exit code.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        mock_check_output.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd="sonic-installer list", output=b"Error: command failed"
        )

        # Act
        rc, msg = image_service.list_images()

        # Assert
        assert rc != 0, "wrong return value"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"],
            stderr=subprocess.STDOUT,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.run")
    def test_image_set_next_boot_success(self, mock_run, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `set_next_boot` method successfully sets the next boot image.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image = "sonic_image"
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        mock_run.return_value = mock_result

        # Act
        rc, msg = image_service.set_next_boot(image)

        # Assert
        assert rc == 0, "wrong return value"
        assert image in msg, "message should contain the name of the new image"
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "set-next-boot", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.run")
    def test_image_set_next_boot_fail_not_exists(self, mock_run, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `set_next_boot` method fails when the image does not exist.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image = "nonexistent_image"
        mock_result = mock.Mock()
        mock_result.returncode = errno.ENOENT
        mock_result.stderr = b"Error: Image does not exist"
        mock_run.return_value = mock_result

        # Act
        rc, msg = image_service.set_next_boot(image)

        # Assert
        assert rc != 0, "wrong return value"
        assert (
            "not" in msg.lower() and ("exist" in msg.lower() or "found" in msg.lower())
        ), "message should contain 'not' and 'exist' or 'found'"
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "set-next-boot", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("subprocess.run")
    def test_image_set_next_boot_fail_not_exists_generic_rc(self, mock_run, MockInit, MockBusName, MockSystemBus):
        """
        Test that the `set_next_boot` method fails when the image does not exist, and sonic-installer returns a generic error code
        instead of errno.ENOENT.
        """
        # Arrange
        image_service = ImageService(mod_name="image_service")
        image = "nonexistent_image"
        mock_result = mock.Mock()
        mock_result.returncode = 1  # returns generic error code
        mock_result.stderr = b"Error: Image does not exist"
        mock_run.return_value = mock_result

        # Act
        rc, msg = image_service.set_next_boot(image)

        # Assert
        assert rc == errno.ENOENT, "wrong return value"
        assert (
            "not" in msg.lower() and ("exist" in msg.lower() or "found" in msg.lower())
        ), "message should contain 'not' and 'exist' or 'found'"
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "set-next-boot", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_valid_json(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that gnoi_install_os handles valid JSON input and returns ERROR_UNIMPLEMENTED.
        """
        image_service = ImageService(mod_name="image_service")
        options = ['{', '"version": "1.0.0"', '}']  # Valid JSON when joined

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 1
        assert "ERROR_UNIMPLEMENTED" in response


    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_invalid_json(self, MockInit, MockBusName, MockSystemBus):
        """
        Test that gnoi_install_os returns error for invalid JSON input.
        """
        image_service = ImageService(mod_name="image_service")
        options = ['{', '"version": "1.0.0"', 'invalid']  # Invalid JSON

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 1
        assert "Invalid InstallRequest JSON" in response
