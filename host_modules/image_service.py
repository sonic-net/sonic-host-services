"""
This module provides services related to SONiC images, including:
1) Calculating checksums for images
2) Listing images
3) Setting the next boot image
4) Handling gNOI OS.Install requests
"""

import errno
import hashlib
import logging
import os
import subprocess
import json
import tarfile

from host_modules import host_service
from host_modules.file_path_policy import DENIED, open_checksum_file
from sonic_py_common import device_info

MOD_NAME = "image_service"

# Host-side directory where the gNOI OS Install front-end (in the gnmi
# container) streams the image before this host service installs it.
# The gnmi container writes to its ImgDir (`--img_dir /mnt/host/var/tmp` in
# gnmi-native.sh), which is the /var/tmp:/mnt/host/var/tmp bind mount from
# rules/docker-gnmi.mk. On the host that same file is at /var/tmp/<version>.
# This constant MUST stay in sync with the container's --img_dir flag.
GNOI_OS_HOST_IMG_DIR = "/var/tmp"

SONIC_INSTALLER = "/usr/local/bin/sonic-installer"

# A SONiC ONIE image is a shell script followed by a tar payload that starts
# after the "exit_marker" line; the payload lists the platforms the image
# supports in installer/platforms_asic.
IMAGE_PAYLOAD_MARKER = b"exit_marker"
IMAGE_HEADER_MAX_SIZE = 1024 * 1024
IMAGE_PLATFORMS_ASIC = "installer/platforms_asic"

# sonic-installer install output that identifies a gNOI InstallError type.
GNOI_INSTALL_ERROR_PATTERNS = [
    ("not a valid SONiC image file", "PARSE_FAIL"),
    ("different platform ASIC type", "INCOMPATIBLE"),
    ("different type than running image", "INCOMPATIBLE"),
    ("Insufficient free disk space", "TOO_LARGE"),
    ("Unable to verify archive checksum", "INTEGRITY_FAIL"),
    ("Failed verify image signature", "INTEGRITY_FAIL"),
]

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
        Handles the gNOI OS Install RPC (backend half).

        The gnmi-container front-end (sonic-gnmi gnmi_server/gnoi_os.go) drives
        the streaming transfer and calls this method twice via D-Bus:

          1) TransferRequest phase -- input {"transferRequest": {"version": ...}}.
             If the version is already installed we reply
             {"validated": {"version": ...}} and the transfer is skipped.
             Otherwise we reply {"transferReady": {}} so the
             front-end starts streaming the image bytes into
             GNOI_OS_HOST_IMG_DIR/<version>. The raw image content is NOT sent
             to this backend; the front-end writes it to the shared img_dir.

          2) TransferEnd phase -- input {"transferEnd": {}}. The image has been
             fully written; we install it with sonic-installer and reply
             {"validated": {"version": ...}}.

        All normal outcomes (including typed InstallError) are returned with
        rc=0 and a protojson-encoded InstallResponse string, because the Go
        DbusApi turns a non-zero rc into a generic gRPC error and only rc=0
        bodies are unmarshalled back into an InstallResponse. A non-zero rc is
        reserved for hard/parse failures.

        Args:
            options: JSON-encoded gNOI os.InstallRequest (delivered as a D-Bus
                array of strings that we re-join).

        Returns:
            (int rc, str response): rc=0 with a protojson InstallResponse on any
            handled outcome; rc=1 with a plain message on unparseable input.
        """
        logger.info("gnoi_install_os: gNOI OS Install RPC called.")

        # The gnmi front-end sends the InstallRequest as a single JSON string.
        # Depending on the D-Bus binding it arrives either as a plain str (real
        # hardware) or as a list/tuple of str fragments (some bindings/tests);
        # concatenate without inserting separators so the JSON is not mangled.
        if isinstance(options, str):
            request_str = options
        else:
            request_str = "".join(options)
        logger.info("gnoi_install_os: InstallRequest JSON string: %s", request_str)

        try:
            request_json = json.loads(request_str)
        except json.JSONDecodeError as e:
            logger.error("gnoi_install_os: Failed to parse InstallRequest JSON: %s", e)
            return 1, f"Invalid InstallRequest JSON: {e}"

        # --- TransferRequest phase -------------------------------------------
        if "transferRequest" in request_json:
            transfer_req = request_json.get("transferRequest") or {}
            version = transfer_req.get("version", "")
            if not version:
                logger.error("gnoi_install_os: TransferRequest missing version")
                self._gnoi_install_version = ""
                return 0, json.dumps(
                    {"installError": {"type": "PARSE_FAIL",
                                      "detail": "TransferRequest must contain a version"}}
                )
            if not self._gnoi_is_safe_version(version):
                logger.error("gnoi_install_os: TransferRequest version %r is not a plain file name", version)
                self._gnoi_install_version = ""
                return 0, json.dumps(
                    {"installError": {"type": "PARSE_FAIL",
                                      "detail": "TransferRequest version must be a plain file name"}}
                )
            images = self._gnoi_list_images()
            if images is not None and version in images["available"]:
                logger.info("gnoi_install_os: version=%s is already installed -> Validated", version)
                self._gnoi_install_version = ""
                return 0, json.dumps({"validated": {"version": version}})
            # Remember the version for the TransferEnd phase. The front-end
            # serializes Install RPCs with a mutex, so a single pending version
            # is sufficient.
            self._gnoi_install_version = version
            logger.info("gnoi_install_os: TransferRequest version=%s -> TransferReady", version)
            return 0, json.dumps({"transferReady": {}})

        # --- TransferEnd phase -----------------------------------------------
        if "transferEnd" in request_json:
            version = getattr(self, "_gnoi_install_version", "")
            if not version:
                logger.error("gnoi_install_os: TransferEnd without a prior TransferRequest")
                return 0, json.dumps(
                    {"installError": {"type": "UNSPECIFIED",
                                      "detail": "TransferEnd received without a prior TransferRequest"}}
                )

            image_path = os.path.join(GNOI_OS_HOST_IMG_DIR, version)
            if not os.path.isfile(image_path):
                logger.error("gnoi_install_os: staged image not found at %s", image_path)
                self._gnoi_install_version = ""
                return 0, json.dumps(
                    {"installError": {"type": "UNSPECIFIED",
                                      "detail": "Transferred image not found at {}".format(image_path)}}
                )

            image_version, install_error = self._gnoi_check_install_image(image_path)
            if install_error is None and image_version != version:
                logger.warning("gnoi_install_os: image transferred as %s holds version %s",
                               version, image_version)
            if install_error is None:
                logger.info("gnoi_install_os: installing image %s (version=%s)", image_path, version)
                cmd = [SONIC_INSTALLER, "install", "-y", image_path]
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode != 0:
                    stdout = result.stdout.decode(errors="replace").strip()
                    stderr = result.stderr.decode(errors="replace").strip()
                    detail = stderr or stdout or "sonic-installer install failed"
                    logger.error("gnoi_install_os: install failed rc=%s: %s", result.returncode, detail)
                    install_error = (self._gnoi_install_error_type(stdout + "\n" + stderr), detail)

            # Best-effort cleanup of the staged image regardless of outcome;
            # sonic-installer copies it into the image store on success.
            try:
                os.remove(image_path)
            except OSError:
                pass
            self._gnoi_install_version = ""

            if install_error is not None:
                error_type, detail = install_error
                return 0, json.dumps(
                    {"installError": {"type": error_type, "detail": detail}}
                )

            # Validated.version is the version the package holds, not the one the
            # client asked for, so that a client can tell when the two differ.
            logger.info("gnoi_install_os: install complete for version=%s", image_version)
            return 0, json.dumps({"validated": {"version": image_version}})

        # --- Anything else ---------------------------------------------------
        logger.error("gnoi_install_os: unsupported InstallRequest type: %s", request_str)
        return 0, json.dumps(
            {"installError": {"type": "PARSE_FAIL",
                              "detail": "Unsupported or unexpected InstallRequest message"}}
        )

    @staticmethod
    def _gnoi_is_safe_version(version):
        """
        Return True if the version can name a file directly in GNOI_OS_HOST_IMG_DIR.

        The version is joined onto GNOI_OS_HOST_IMG_DIR to locate, install and
        then delete the staged image, so it must not be able to name anything
        outside that directory.
        """
        return (isinstance(version, str)
                and version not in (".", "..")
                and "/" not in version
                and "\0" not in version)

    def _gnoi_check_install_image(self, image_path):
        """
        Reject a staged image that sonic-installer would install anyway or
        fail on without saying why.

        Returns:
            (image version, error): the OS version read from the image, "" if
            it cannot be read, and (InstallError type, detail) if the image
            must be rejected, else None.
        """
        result = subprocess.run(
            [SONIC_INSTALLER, "binary-version", image_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        image_version = result.stdout.decode(errors="replace").strip() if result.returncode == 0 else ""
        if not image_version:
            return "", ("PARSE_FAIL",
                        "Unable to read the OS version from the transferred image; it is not a valid SONiC image")

        images = self._gnoi_list_images()
        if images is not None and image_version == images["current"]:
            return image_version, ("INSTALL_RUN_PACKAGE",
                                   "Image {} is the currently running OS version".format(image_version))

        platforms = self._read_image_platforms(image_path)
        platform = device_info.get_platform()
        if platforms is not None and platform and platform not in platforms:
            return image_version, ("INCOMPATIBLE",
                                   "Image {} does not support platform {}".format(image_version, platform))
        return image_version, None

    def _gnoi_list_images(self):
        """
        Returns the parsed `sonic-installer list` output, or None if it cannot be read.
        """
        try:
            output = subprocess.check_output([SONIC_INSTALLER, "list"], stderr=subprocess.STDOUT).decode().strip()
        except (OSError, subprocess.CalledProcessError) as e:
            logger.warning("gnoi_install_os: unable to list installed images: %s", e)
            return None
        return self._parse_sonic_installer_list(output)

    def _read_image_platforms(self, image_path):
        """
        Returns the set of platforms listed in the image payload, or None if
        the image does not carry the list.
        """
        try:
            with open(image_path, "rb") as f:
                header_size = 0
                while header_size < IMAGE_HEADER_MAX_SIZE:
                    line = f.readline(IMAGE_HEADER_MAX_SIZE)
                    if not line:
                        return None
                    header_size += len(line)
                    if line.rstrip(b"\n") == IMAGE_PAYLOAD_MARKER:
                        break
                else:
                    return None
                with tarfile.open(fileobj=f, mode="r|*") as tar:
                    for member in tar:
                        if member.name == IMAGE_PLATFORMS_ASIC:
                            member_file = tar.extractfile(member)
                            if member_file is None:
                                return None
                            data = member_file.read().decode(errors="replace")
                            return set(data.split())
        except (OSError, tarfile.TarError) as e:
            logger.warning("gnoi_install_os: unable to read %s from %s: %s", IMAGE_PLATFORMS_ASIC, image_path, e)
        return None

    def _gnoi_install_error_type(self, output):
        """Map sonic-installer install output to a gNOI InstallError type."""
        for pattern, error_type in GNOI_INSTALL_ERROR_PATTERNS:
            if pattern in output:
                return error_type
        return "UNSPECIFIED"
