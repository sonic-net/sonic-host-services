"""
This module provides services related to SONiC images, including:
1) Downloading images
2) Installing images
3) Calculating checksums for images
"""

import errno
import hashlib
import logging
import os
import requests
import shutil
import subprocess
import json
import tempfile
from urllib.parse import urlsplit

from host_modules import host_service
from host_modules.file_path_policy import check_read_path, open_checksum_file
from host_modules.image_store import DENIED, EXPORT_ROOT, ImageStore, artifact_parts

MOD_NAME = "image_service"

DEFAULT_IMAGE_SAVE_AS = EXPORT_ROOT + "/downloaded-sonic.bin"
INSTALL_STAGING_PARENT = "/host"

logger = logging.getLogger(__name__)


class ImageService(host_service.HostModule):
    """DBus endpoint that handles downloading and installing SONiC images"""

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="ss", out_signature="is"
    )
    def download(self, image_url, save_as):
        """
        Download a SONiC image.

        Args:
             image_url: url for remote image.
             save_as: destination beneath the managed image export directory.
        """
        logger.info("Download new sonic image from {} as {}".format(image_url, save_as))
        try:
            artifact_parts(save_as)
        except PermissionError:
            logger.warning("Denied download outside managed image exports")
            return errno.EACCES, DENIED
        try:
            response = requests.get(image_url, stream=True)
            if response.status_code != 200:
                logger.error(
                    "Failed to download image: HTTP status code {}".format(
                        response.status_code
                    )
                )
                return errno.EIO, "HTTP error: {}".format(response.status_code)

            with ImageStore().create(save_as) as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
            return 0, "Download successful"
        except PermissionError:
            logger.warning("Denied managed-image download")
            return errno.EACCES, DENIED
        except Exception as e:
            logger.error("Failed to write downloaded image to disk: {}".format(e))
            return errno.EIO, str(e)

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def install(self, where):
        """
        Install a managed local image or a remote HTTP(S) image.

        Args:
            where: a managed artifact path or an HTTP(S) image URL.
        """
        logger.info("Installing a SONiC image")
        try:
            if urlsplit(where).scheme.lower() in ("http", "https"):
                return self._run_installer(where)
            check_read_path(where)
            with open_checksum_file(where) as source:
                # sonic-installer may reopen its input in descendants. Give it
                # a private copy, never a caller-selected privileged pathname.
                with tempfile.TemporaryDirectory(
                        prefix=".sonic-image-install-", dir=INSTALL_STAGING_PARENT) as directory:
                    image = os.path.join(directory, "image.bin")
                    descriptor = os.open(
                        image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    return self._run_installer(image)
        except PermissionError:
            logger.warning("Denied installation from an unavailable image artifact")
            return errno.EACCES, DENIED
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.exception("Image installation failed")
            return errno.EIO, "Image installation failed"

    @staticmethod
    def _run_installer(where):
        cmd = ["/usr/local/bin/sonic-installer", "install", "-y", where]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ""
        if result.returncode:
            lines = result.stderr.decode().split("\n")
            for line in lines:
                if "Error" in line:
                    msg = line
                    break
        return result.returncode, msg

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

        try:
            check_read_path(file_path)
        except PermissionError:
            logger.warning("Denied checksum outside managed image exports")
            return errno.EACCES, DENIED

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
            logger.warning("Denied checksum access to an unavailable image artifact")
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
