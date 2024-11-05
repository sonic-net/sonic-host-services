import errno
import docker
from unittest import mock
from host_modules.docker_service import DockerService

MOD_NAME = "docker_service"


class TestDockerService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_stop_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.stop.return_value = None

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.stop("container_name")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.stop.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_stop_fail_not_exist(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "Container not found"
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.stop("non_existent_container")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with(
            "non_existent_container"
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_stop_fail_api_error(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.stop.side_effect = (
            docker.errors.APIError("API error")
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.stop("container_name")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.stop.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_kill_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.kill.return_value = None

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.kill("container_name")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.kill.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_kill_fail_not_found(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "Container not found"
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.kill("non_existent_container")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with(
            "non_existent_container"
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_kill_fail_api_error(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.kill.side_effect = (
            docker.errors.APIError("API error")
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.kill("container_name")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.kill.assert_called_once()
    
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_restart_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.restart.return_value = None

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.restart("container_name")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.restart.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_restart_fail_not_found(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound(
            "Container not found"
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.restart("non_existent_container")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with(
            "non_existent_container"
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_restart_fail_api_error(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.restart.side_effect = (
            docker.errors.APIError("API error")
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.restart("container_name")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("container_name")
        mock_docker_client.containers.get.return_value.restart.assert_called_once()
