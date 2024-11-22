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
