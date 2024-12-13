"""Docker service handler"""

from host_modules import host_service
import docker
import signal
import errno
import logging

MOD_NAME = "docker_service"

# The set of allowed containers that can be managed by this service.
ALLOWED_CONTAINERS = {
    "syncd",
    "acms",
    "gnmi",
    "telemetry",
    "snmp",
    "pmon",
    "lldp",
    "dhcp_relay",
    "radv",
    "teamd",
    "bgp",
    "swss",
    "restapi",
    "eventd",
    "database",
}

# The set of allowed images that can be managed by this service.
ALLOWED_IMAGES = {
    "docker-syncd-brcm",
    "docker-syncd-cisco" "docker-acms",
    "docker-sonic-gnmi",
    "docker-sonic-telemetry",
    "docker-snmp",
    "docker-platform-monitor",
    "docker-lldp",
    "docker-dhcp-relay",
    "docker-router-advertiser",
    "docker-teamd",
    "docker-fpm-frr",
    "docker-orchagent",
    "docker-sonic-restapi",
    "docker-eventd",
    "docker-database",
}


def is_allowed_image(image):
    """
    Check if the image is allowed to be managed by this service.

    Args:
        image (str): The image name.

    Returns:
        bool: True if the image is allowed, False otherwise.
    """
    image_name = image.split(":")[0]  # Remove tag if present
    return image_name in ALLOWED_IMAGES


def get_sonic_container(container_id):
    """
    Get a Sonic docker container by name. If the container is not a Sonic container, raise PermissionError.
    """
    client = docker.from_env()
    if container_id not in ALLOWED_CONTAINERS:
        raise PermissionError(
            "Container {} is not allowed to be managed by this service.".format(
                container_id
            )
        )
    container = client.containers.get(container_id)
    return container


class DockerService(host_service.HostModule):
    """
    DBus endpoint that executes the docker command
    """

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def stop(self, container_id):
        """
        Stop a running Docker container.

        Args:
            container_id (str): The name of the Docker container.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            container = get_sonic_container(container_id)
            container.stop()
            return 0, "Container {} has been stopped.".format(container.name)
        except PermissionError:
            msg = "Container {} is not allowed to be managed by this service.".format(
                container_id
            )
            logging.error(msg)
            return errno.EPERM, msg
        except docker.errors.NotFound:
            msg = "Container {} does not exist.".format(container_id)
            logging.error(msg)
            return errno.ENOENT, msg
        except Exception as e:
            msg = "Failed to stop container {}: {}".format(container_id, str(e))
            logging.error(msg)
            return 1, msg

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="si", out_signature="is"
    )
    def kill(self, container_id, signal=signal.SIGKILL):
        """
        Kill or send a signal to a running Docker container.

        Args:
            container_id (str): The name or ID of the Docker container.
            signal (int): The signal to send. Defaults to SIGKILL.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            container = get_sonic_container(container_id)
            container.kill(signal=signal)
            return 0, "Container {} has been killed with signal {}.".format(
                container.name, signal
            )
        except PermissionError:
            msg = "Container {} is not allowed to be managed by this service.".format(
                container_id
            )
            logging.error(msg)
            return errno.EPERM, msg
        except docker.errors.NotFound:
            msg = "Container {} does not exist.".format(container_id)
            logging.error(msg)
            return errno.ENOENT, msg
        except Exception as e:
            return 1, "Failed to kill container {}: {}".format(container_id, str(e))

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def restart(self, container_id):
        """
        Restart a running Docker container.

        Args:
            container_id (str): The name or ID of the Docker container.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            container = get_sonic_container(container_id)
            container.restart()
            return 0, "Container {} has been restarted.".format(container.name)
        except PermissionError:
            return (
                errno.EPERM,
                "Container {} is not allowed to be managed by this service.".format(
                    container_id
                ),
            )
        except docker.errors.NotFound:
            return errno.ENOENT, "Container {} does not exist.".format(container_id)
        except Exception as e:
            return 1, "Failed to restart container {}: {}".format(container_id, str(e))

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="ssa{sv}", out_signature="is"
    )
    def run(self, image, command, kwargs):
        """
        Run a Docker container.

        Args:
            image (str): The name of the Docker image to run.
            command (str): The command to run in the container
            kwargs (dict): Additional keyword arguments to pass to the Docker API.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            client = docker.from_env()

            if not is_allowed_image(image):
                return (
                    errno.EPERM,
                    "Image {} is not allowed to be managed by this service.".format(
                        image
                    ),
                )

            if command:
                return (
                    errno.EPERM,
                    "Only an empty string command is allowed. Non-empty commands are not permitted by this service.",
                )

            # Semgrep cannot detect codes for validating image and command.
            # nosemgrep: python.docker.security.audit.docker-arbitrary-container-run.docker-arbitrary-container-run
            container = client.containers.run(image, command, **kwargs)
            return 0, "Container {} has been started.".format(container.name)
        except docker.errors.ImageNotFound:
            return errno.ENOENT, "Image {} not found.".format(image)
        except Exception as e:
            return 1, "Failed to run image {}: {}".format(image, str(e))
