"""Docker service handler"""

from host_modules import host_service
import docker
import signal
import errno

MOD_NAME = "docker_service"

# The set of allowed containers that can be managed by this service.
# First element is the image name, second element is the container name.
ALLOWED_CONTAINERS = [
    ("docker-syncd-brcm", "syncd"),
    ("docker-acms", "acms"),
    ("docker-sonic-gnmi", "gnmi"),
    ("docker-sonic-telemetry", "telemetry"),
    ("docker-snmp", "snmp"),
    ("docker-platform-monitor", "pmon"),
    ("docker-lldp", "lldp"),
    ("docker-dhcp-relay", "dhcp_relay"),
    ("docker-router-advertiser", "radv"),
    ("docker-teamd", "teamd"),
    ("docker-fpm-frr", "bgp"),
    ("docker-orchagent", "swss"),
    ("docker-sonic-restapi", "restapi"),
    ("docker-eventd", "eventd"),
    ("docker-database", "database"),
]


def is_allowed_container(container):
    """
    Check if the container is allowed to be managed by this service.

    Args:
        container (str): The container name.

    Returns:
        bool: True if the container is allowed, False otherwise.
    """
    for _, allowed_container in ALLOWED_CONTAINERS:
        if container == allowed_container:
            return True
    return False


class DockerService(host_service.HostModule):
    """
    DBus endpoint that executes the docker command
    """

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def stop(self, container):
        """
        Stop a running Docker container.

        Args:
            container (str): The name or ID of the Docker container.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            client = docker.from_env()
            if not is_allowed_container(container):
                return (
                    errno.EPERM,
                    "Container {} is not allowed to be managed by this service.".format(
                        container
                    ),
                )
            container = client.containers.get(container)
            container.stop()
            return 0, "Container {} has been stopped.".format(container.name)
        except docker.errors.NotFound:
            return errno.ENOENT, "Container {} does not exist.".format(container)
        except Exception as e:
            return 1, "Failed to stop container {}: {}".format(container, str(e))

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="si", out_signature="is"
    )
    def kill(self, container, signal=signal.SIGKILL):
        """
        Kill or send a signal to a running Docker container.

        Args:
            container (str): The name or ID of the Docker container.
            signal (int): The signal to send. Defaults to SIGKILL.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            client = docker.from_env()
            if not is_allowed_container(container):
                return (
                    errno.EPERM,
                    "Container {} is not allowed to be managed by this service.".format(
                        container
                    ),
                )
            container = client.containers.get(container)
            container.kill(signal=signal)
            return 0, "Container {} has been killed with signal {}.".format(
                container.name, signal
            )
        except docker.errors.NotFound:
            return errno.ENOENT, "Container {} does not exist.".format(container)
        except Exception as e:
            return 1, "Failed to kill container {}: {}".format(container, str(e))

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="s", out_signature="is"
    )
    def restart(self, container):
        """
        Restart a running Docker container.

        Args:
            container (str): The name or ID of the Docker container.

        Returns:
            tuple: A tuple containing the exit code (int) and a message indicating the result of the operation.
        """
        try:
            client = docker.from_env()
            if not is_allowed_container(container):
                return (
                    errno.EPERM,
                    "Container {} is not allowed to be managed by this service.".format(
                        container
                    ),
                )
            container = client.containers.get(container)
            container.restart()
            return 0, "Container {} has been restarted.".format(container.name)
        except docker.errors.NotFound:
            return errno.ENOENT, "Container {} does not exist.".format(container)
        except Exception as e:
            return 1, "Failed to restart container {}: {}".format(container, str(e))

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
            if not DockerService.validate_image(image):
                return errno.EPERM, "Image {} is not allowed.".format(image)
            container = client.containers.run(image, command, **kwargs)
            return 0, "Container {} has been started.".format(container.name)
        except docker.errors.ImageNotFound:
            return errno.ENOENT, "Image {} not found.".format(image)
        except Exception as e:
            return 1, "Failed to run container {}: {}".format(image, str(e))

    @staticmethod
    def get_used_images_name():
        """
        Get the list of used images.

        Returns:
            list: A list of image names.
        """
        try:
            client = docker.from_env()
            images = client.images.list(all=True)
            return list(set(image.tags[0].split(":")[0] for image in images if image.tags))
        except Exception as e:
            return "Failed to get used images: {}".format(str(e))

    @staticmethod
    def validate_image(image):
        """
        Validate the image name.

        Args:
            image (str): The name of the Docker image.

        Returns:
            bool: True if the image is allowed to be use for run/create command.
        """
        base_image_name = image.split(":")[0]
        known_images = DockerService.get_used_images_name()
        return base_image_name in known_images