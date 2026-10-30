"""
This module provides services related to SONiC images, including:
1) Calculating checksums for images
2) Listing images
3) Setting the next boot image
4) Handling gNOI OS.Install requests (not yet implemented)
"""

import errno
import hashlib
import logging
import subprocess
import json

from host_modules import host_service
from host_modules.file_path_policy import DENIED, open_checksum_file

MOD_NAME = "image_service"

logger = logging.getLogger(__name__)


class ImageService(host_service.HostModule):
    """DBus endpoint that handles SONiC image operations"""

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="ss", out_signature="is"
    )
    def checksum(self, file_path, algorithm):
        """
        Calculate the checksum of a file.

        Args:
            file_path: path to the file.
            algorithm: checksum algorithm to use (sha256, sha512, md5).
        """

        logger.info("Calculating {} checksum for file {}".format(algorithm, file_path))

        hash_func = None
        if algorithm == "sha256":
            hash_func = hashlib.sha256()
        elif algorithm == "sha512":
            hash_func = hashlib.sha512()
        elif algorithm == "md5":
            hash_func = hashlib.md5()
        else:
            logger.error("Unsupported algorithm: {}".format(algorithm))
            return errno.EINVAL, "Unsupported algorithm"

        try:
            with open_checksum_file(file_path) as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_func.update(chunk)
            return 0, hash_func.hexdigest()
        except PermissionError:
            logger.warning("Denied checksum of a non-public file")
            return errno.EACCES, DENIED
        except Exception as e:
            logger.error("Failed to calculate checksum: {}".format(e))
            return errno.EIO, str(e)

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="", out_signature="is"
    )
    def list_images(self):
        """
        List the current, next, and available SONiC images.

        Returns:
            A tuple with an error code and a JSON string with keys "current", "next", and "available" or an error message.
        """
        logger.info("Listing SONiC images")

        try:
            output = subprocess.check_output(
                ["/usr/local/bin/sonic-installer", "list"],
                stderr=subprocess.STDOUT,
            ).decode().strip()
            result = self._parse_sonic_installer_list(output)
            logger.info("List result: {}".format(result))
            return 0, json.dumps(result)
        except subprocess.CalledProcessError as e:
            msg = "Failed to list images: command {} failed with return code {} and message {}".format(e.cmd, e.returncode, e.output.decode())
            logger.error(msg)
            return e.returncode, msg

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def set_next_boot(self, image):
        """
        Set the image to be used for the next boot.

        Args:
            image: The name of the image to set for the next boot.
        """
        logger.info("Setting the next boot image to {}".format(image))
        cmd = ["/usr/local/bin/sonic-installer", "set-next-boot", image]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = "Boot image set to {}".format(image)
        logger.info(msg)
        if result.returncode:
            logger.error("Failed to set next boot image: {}".format(result.stderr.decode()))
            msg = result.stderr.decode()
            # sonic-installer might not return a proper error code, so we need to check the message.
            if "not" in msg.lower() and ("exist" in msg.lower() or "found" in msg.lower()):
                return errno.ENOENT, msg
        return result.returncode, msg


    def _parse_sonic_installer_list(self, output):
        """
        Parse the output of the sonic-installer list command.

        Args:
            output: The output of the sonic-installer list command.

        Returns:
            A dictionary with keys "current", "next", and "available" containing the respective images.
        """
        current_image = ""
        next_image = ""
        available_images = []

        for line in output.split("\n"):
            if "current:" in line.lower():
                parts = line.split(":")
                if len(parts) > 1:
                    current_image = parts[1].strip()
            elif "next:" in line.lower():
                parts = line.split(":")
                if len(parts) > 1:
                    next_image = parts[1].strip()
            elif "available:" in line.lower():
                continue
            else:
                available_images.append(line.strip())

        logger.info("Current image: {}".format(current_image))
        logger.info("Next image: {}".format(next_image))
        logger.info("Available images: {}".format(available_images))
        return {
            "current": current_image or "",
            "next": next_image or "",
            "available": available_images or [],
        }

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is"
    )
    def gnoi_install_os(self, options):
        """
        Handles the gNOI OS Install RPC.

        Args:
            options (str): JSON-formatted InstallRequest from os.proto

        Returns:
            int: 0 on success
            str: JSON-encoded InstallResponse with TransferReady or TransferProgress or Validated msg
        """
        logger.info("installos: gNOI OS Install RPC called.")

        # Join list of strings into one JSON string
        request_str = " ".join(options)
        logger.info("InstallRequest JSON string: %s", request_str)

        try:
            request_json = json.loads(request_str)
            logger.info("Parsed InstallRequest: %s", json.dumps(request_json, indent=2))
        except json.JSONDecodeError as e:
            logger.error("Failed to parse InstallRequest JSON: %s", e)
            return 1, f"Invalid InstallRequest JSON: {e}"

        return 1, "ERROR_UNIMPLEMENTED: OS Install not supported"
