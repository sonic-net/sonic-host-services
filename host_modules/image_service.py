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
import stat
import subprocess

from host_modules import host_service
import tempfile

MOD_NAME = "image_service"

DEFAULT_IMAGE_SAVE_AS = "/tmp/downloaded-sonic.bin"

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
             save_as: local path for the downloaded image. The directory must exist and be *all* writable.
        """
        logger.info("Download new sonic image from {} as {}".format(image_url, save_as))
        # Check if the directory exists, is absolute and has write permission.
        if not os.path.isabs(save_as):
            logger.error("The path {} is not an absolute path".format(save_as))
            return errno.EINVAL, "Path is not absolute"
        dir = os.path.dirname(save_as)
        if not os.path.isdir(dir):
            logger.error("Directory {} does not exist".format(dir))
            return errno.ENOENT, "Directory does not exist"
        st_mode = os.stat(dir).st_mode
        if (
            not (st_mode & stat.S_IWUSR)
            or not (st_mode & stat.S_IWGRP)
            or not (st_mode & stat.S_IWOTH)
        ):
            logger.error("Directory {} is not all writable {}".format(dir, st_mode))
            return errno.EACCES, "Directory is not all writable"
        try:
            response = requests.get(image_url, stream=True)
            if response.status_code != 200:
                logger.error(
                    "Failed to download image: HTTP status code {}".format(
                        response.status_code
                    )
                )
                return errno.EIO, "HTTP error: {}".format(response.status_code)

            with tempfile.NamedTemporaryFile(dir="/tmp", delete=False) as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                temp_file_path = tmp_file.name
            os.replace(temp_file_path, save_as)
            return 0, "Download successful"
        except Exception as e:
            logger.error("Failed to write downloaded image to disk: {}".format(e))
            return errno.EIO, str(e)

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def install(self, where):
        """
        Install a a sonic image:

        Args:
            where: either a local path or a remote url pointing to the image.
        """
        logger.info("Using sonic-installer to install the image at {}.".format(where))
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

        if not os.path.isfile(file_path):
            logger.error("File {} does not exist".format(file_path))
            return errno.ENOENT, "File does not exist"

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
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_func.update(chunk)
            return 0, hash_func.hexdigest()
        except Exception as e:
            logger.error("Failed to calculate checksum: {}".format(e))
            return errno.EIO, str(e)
