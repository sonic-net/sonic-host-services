import hashlib
import io
import subprocess
import os
import tarfile
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

    INSTALLED_IMAGES_LIST = (
        b"Current: SONiC-OS-running\nNext: SONiC-OS-running\nAvailable:\nSONiC-OS-running\nSONiC-OS-other\n"
    )

    @mock.patch("subprocess.check_output", return_value=INSTALLED_IMAGES_LIST)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request(self, MockInit, MockBusName, MockSystemBus, mock_check_output):
        """
        TransferRequest with a version should return TransferReady and remember
        the version for the subsequent TransferEnd.
        """
        image_service = ImageService(mod_name="image_service")
        options = ['{"transferRequest": {"version": "SONiC.1.0.0"}}']

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 0
        assert json.loads(response) == {"transferReady": {}}
        assert image_service._gnoi_install_version == "SONiC.1.0.0"
        mock_check_output.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "list"], stderr=subprocess.STDOUT
        )

    @mock.patch("subprocess.check_output", return_value=INSTALLED_IMAGES_LIST)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request_string_input(
        self, MockInit, MockBusName, MockSystemBus, mock_check_output
    ):
        """
        On real hardware the D-Bus arg arrives as a single string (not a list);
        it must be used as-is and not mangled character-by-character.
        """
        image_service = ImageService(mod_name="image_service")
        options = '{"transferRequest": {"version": "SONiC.1.0.0"}}'

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 0
        assert json.loads(response) == {"transferReady": {}}
        assert image_service._gnoi_install_version == "SONiC.1.0.0"

    @pytest.mark.parametrize("version", ["SONiC-OS-running", "SONiC-OS-other"])
    @mock.patch("subprocess.check_output", return_value=INSTALLED_IMAGES_LIST)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request_already_installed(
        self, MockInit, MockBusName, MockSystemBus, mock_check_output, version
    ):
        """
        TransferRequest for an installed version should return Validated so the
        image is not transferred again.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "stale"
        options = ['{"transferRequest": {"version": "%s"}}' % version]

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 0
        assert json.loads(response) == {"validated": {"version": version}}
        assert image_service._gnoi_install_version == ""

    @mock.patch(
        "subprocess.check_output",
        side_effect=subprocess.CalledProcessError(1, "sonic-installer list", output=b""),
    )
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request_list_fails(
        self, MockInit, MockBusName, MockSystemBus, mock_check_output
    ):
        """
        If the installed images cannot be listed, the image is transferred.
        """
        image_service = ImageService(mod_name="image_service")

        rc, response = image_service.gnoi_install_os(['{"transferRequest": {"version": "SONiC-OS-running"}}'])

        assert rc == 0
        assert json.loads(response) == {"transferReady": {}}
        assert image_service._gnoi_install_version == "SONiC-OS-running"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request_missing_version(self, MockInit, MockBusName, MockSystemBus):
        """
        TransferRequest without a version should return a PARSE_FAIL InstallError.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "SONiC-OS-pending"
        options = ['{"transferRequest": {}}']

        rc, response = image_service.gnoi_install_os(options)

        assert rc == 0
        resp = json.loads(response)
        assert resp["installError"]["type"] == "PARSE_FAIL"
        assert image_service._gnoi_install_version == ""

    @pytest.mark.parametrize(
        "version",
        ["../../etc/shadow", "/etc/shadow", "sub/SONiC.1.0.0", ".", "..", "SONiC\u0000.bin", 5, ["SONiC.1.0.0"]],
    )
    @mock.patch("os.remove")
    @mock.patch("subprocess.run")
    @mock.patch("subprocess.check_output")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_request_unsafe_version(
        self, MockInit, MockBusName, MockSystemBus, mock_check_output, mock_run, mock_remove, version
    ):
        """
        A version that could name a file outside GNOI_OS_HOST_IMG_DIR is rejected
        with PARSE_FAIL, and clears any pending version so TransferEnd never
        installs or removes a file for it.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "SONiC.pending"

        rc, response = image_service.gnoi_install_os(
            [json.dumps({"transferRequest": {"version": version}})])

        assert rc == 0
        assert json.loads(response)["installError"]["type"] == "PARSE_FAIL"
        assert image_service._gnoi_install_version == ""
        mock_check_output.assert_not_called()

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        assert json.loads(response)["installError"]["type"] == "UNSPECIFIED"
        mock_run.assert_not_called()
        mock_remove.assert_not_called()

    @pytest.mark.parametrize(
        "requested, image_version",
        [
            ("SONiC.1.0.0", "SONiC.1.0.0"),
            ("SONiC-OS-FOO", "SONiC-OS-master.1761563-5e13a932"),
        ],
    )
    @mock.patch("host_modules.image_service.ImageService._gnoi_check_install_image")
    @mock.patch("subprocess.run")
    @mock.patch("os.remove")
    @mock.patch("os.path.isfile", return_value=True)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_end_success(
        self, MockInit, MockBusName, MockSystemBus, mock_isfile, mock_remove, mock_run, mock_check,
        requested, image_version
    ):
        """
        TransferEnd should install the staged image and return Validated with the
        version the image holds, whatever version the client transferred it as.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = requested
        mock_check.return_value = (image_version, None)
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = b""
        mock_result.stderr = b""
        mock_run.return_value = mock_result

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        assert json.loads(response) == {"validated": {"version": image_version}}
        mock_check.assert_called_once_with("/var/tmp/" + requested)
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "install", "-y", "/var/tmp/" + requested],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # pending version is cleared after install
        assert image_service._gnoi_install_version == ""

    @pytest.mark.parametrize(
        "stdout, stderr, expected_type",
        [
            (b"", b"Image file does not exist or is not a valid SONiC image file\nAborted!", "PARSE_FAIL"),
            (b"", b"Image file 'x' is of a different platform ASIC type than running platform's.\nAborted!",
             "INCOMPATIBLE"),
            (b"", b"Image file 'x' is of a different type than running image.\nAborted!", "INCOMPATIBLE"),
            (b"", b"Insufficient free disk space to install the image. Aborting...\nAborted!", "TOO_LARGE"),
            (b"Verifying image checksum ...\nERROR: Unable to verify archive checksum\n", b"", "INTEGRITY_FAIL"),
            (b"", b"Error: Failed verify image signature\nAborted!", "INTEGRITY_FAIL"),
            (b"", b"Error: bad image", "UNSPECIFIED"),
        ],
    )
    @mock.patch("host_modules.image_service.ImageService._gnoi_check_install_image",
                return_value=("SONiC.bad", None))
    @mock.patch("subprocess.run")
    @mock.patch("os.remove")
    @mock.patch("os.path.isfile", return_value=True)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_end_install_failure(
        self, MockInit, MockBusName, MockSystemBus, mock_isfile, mock_remove, mock_run, mock_check,
        stdout, stderr, expected_type
    ):
        """
        A sonic-installer failure on TransferEnd should return an InstallError typed from the
        installer output (rc=0 so the front-end sees the typed error).
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "SONiC.bad"
        mock_result = mock.Mock()
        mock_result.returncode = 1
        mock_result.stdout = stdout
        mock_result.stderr = stderr
        mock_run.return_value = mock_result

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        resp = json.loads(response)
        assert resp["installError"]["type"] == expected_type
        assert resp["installError"]["detail"] == (stderr or stdout).decode().strip()
        mock_remove.assert_called_once_with("/var/tmp/SONiC.bad")
        assert image_service._gnoi_install_version == ""

    @mock.patch(
        "host_modules.image_service.ImageService._gnoi_check_install_image",
        return_value=("SONiC.x", ("INCOMPATIBLE", "Image SONiC.x does not support platform p")),
    )
    @mock.patch("subprocess.run")
    @mock.patch("os.remove")
    @mock.patch("os.path.isfile", return_value=True)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_end_rejected_before_install(
        self, MockInit, MockBusName, MockSystemBus, mock_isfile, mock_remove, mock_run, mock_check
    ):
        """
        An image rejected by the pre-install checks is not handed to sonic-installer install.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "SONiC.x"

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        assert json.loads(response) == {
            "installError": {"type": "INCOMPATIBLE", "detail": "Image SONiC.x does not support platform p"}
        }
        mock_run.assert_not_called()
        mock_remove.assert_called_once_with("/var/tmp/SONiC.x")
        assert image_service._gnoi_install_version == ""

    @pytest.mark.parametrize(
        "binary_version_rc, binary_version_out, platforms, expected",
        [
            (1, b"Image file does not exist or is not a valid SONiC image file\n", None, "PARSE_FAIL"),
            (0, b"\n", None, "PARSE_FAIL"),
            (0, b"SONiC-OS-running\n", None, "INSTALL_RUN_PACKAGE"),
            (0, b"SONiC-OS-new\n", {"x86_64-other-r0"}, "INCOMPATIBLE"),
            (0, b"SONiC-OS-new\n", {"x86_64-other-r0", "arm64-this-r0"}, None),
            (0, b"SONiC-OS-new\n", None, None),
        ],
    )
    @mock.patch("host_modules.image_service.device_info.get_platform", return_value="arm64-this-r0")
    @mock.patch("host_modules.image_service.ImageService._read_image_platforms")
    @mock.patch("subprocess.check_output")
    @mock.patch("subprocess.run")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_check_install_image(
        self, MockInit, MockBusName, MockSystemBus, mock_run, mock_check_output, mock_read_platforms,
        mock_get_platform, binary_version_rc, binary_version_out, platforms, expected
    ):
        """
        The pre-install checks map an unreadable image to PARSE_FAIL, the running version to
        INSTALL_RUN_PACKAGE and an image without the running platform to INCOMPATIBLE.
        """
        image_service = ImageService(mod_name="image_service")
        mock_run.return_value = mock.Mock(returncode=binary_version_rc, stdout=binary_version_out, stderr=b"")
        mock_check_output.return_value = (
            b"Current: SONiC-OS-running\nNext: SONiC-OS-running\nAvailable:\nSONiC-OS-running\n"
        )
        mock_read_platforms.return_value = platforms

        version, error = image_service._gnoi_check_install_image("/var/tmp/image")

        assert (error[0] if error else None) == expected
        assert version == (binary_version_out.decode().strip() if binary_version_rc == 0 else "")
        mock_run.assert_called_once_with(
            ["/usr/local/bin/sonic-installer", "binary-version", "/var/tmp/image"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @mock.patch("host_modules.image_service.device_info.get_platform", return_value="arm64-this-r0")
    @mock.patch("host_modules.image_service.ImageService._read_image_platforms", return_value=None)
    @mock.patch("subprocess.check_output")
    @mock.patch("subprocess.run")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_check_install_image_list_fails(
        self, MockInit, MockBusName, MockSystemBus, mock_run, mock_check_output, mock_read_platforms,
        mock_get_platform
    ):
        """
        Failing to read the running image does not block the install.
        """
        image_service = ImageService(mod_name="image_service")
        mock_run.return_value = mock.Mock(returncode=0, stdout=b"SONiC-OS-new\n", stderr=b"")
        mock_check_output.side_effect = subprocess.CalledProcessError(1, "sonic-installer list", output=b"")

        assert image_service._gnoi_check_install_image("/var/tmp/image") == ("SONiC-OS-new", None)

    @staticmethod
    def _make_image(path, header=b"#!/bin/sh\necho installer\nexit 0\nexit_marker\n", platforms=None):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as tar:
            for name, data in [("installer/install.sh", b"echo install\n")] + (
                [("installer/platforms_asic", platforms)] if platforms is not None else []
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with open(path, "wb") as f:
            f.write(header + payload.getvalue())

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_read_image_platforms(self, MockInit, MockBusName, MockSystemBus, tmp_path):
        """
        The platform list is read from installer/platforms_asic in the image payload.
        """
        image_service = ImageService(mod_name="image_service")
        image = str(tmp_path / "image.bin")
        self._make_image(image, platforms=b"x86_64-a-r0\narm64-b-r0\n")

        assert image_service._read_image_platforms(image) == {"x86_64-a-r0", "arm64-b-r0"}

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_read_image_platforms_without_list(self, MockInit, MockBusName, MockSystemBus, tmp_path):
        """
        Images without installer/platforms_asic, without a payload marker, with an unreadable
        payload or with a platforms_asic that is not a file carry no platform list.
        """
        image_service = ImageService(mod_name="image_service")
        no_list = str(tmp_path / "no_list.bin")
        self._make_image(no_list)
        no_marker = tmp_path / "no_marker.bin"
        no_marker.write_bytes(os.urandom(4096))
        bad_payload = tmp_path / "bad_payload.bin"
        bad_payload.write_bytes(b"#!/bin/sh\nexit_marker\n" + b"\x00garbage" * 100)
        list_is_dir = tmp_path / "list_is_dir.bin"
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as tar:
            info = tarfile.TarInfo("installer/platforms_asic")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        list_is_dir.write_bytes(b"#!/bin/sh\nexit_marker\n" + payload.getvalue())

        assert image_service._read_image_platforms(no_list) is None
        assert image_service._read_image_platforms(str(no_marker)) is None
        assert image_service._read_image_platforms(str(bad_payload)) is None
        assert image_service._read_image_platforms(str(list_is_dir)) is None
        assert image_service._read_image_platforms(str(tmp_path / "missing.bin")) is None

    @mock.patch("os.path.isfile", return_value=False)
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_end_missing_image(
        self, MockInit, MockBusName, MockSystemBus, mock_isfile
    ):
        """
        TransferEnd when the staged image is absent should return an InstallError.
        """
        image_service = ImageService(mod_name="image_service")
        image_service._gnoi_install_version = "SONiC.1.0.0"

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        resp = json.loads(response)
        assert resp["installError"]["type"] == "UNSPECIFIED"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_gnoi_install_os_transfer_end_without_request(self, MockInit, MockBusName, MockSystemBus):
        """
        TransferEnd with no prior TransferRequest should return an InstallError.
        """
        image_service = ImageService(mod_name="image_service")

        rc, response = image_service.gnoi_install_os(['{"transferEnd": {}}'])

        assert rc == 0
        resp = json.loads(response)
        assert resp["installError"]["type"] == "UNSPECIFIED"

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
