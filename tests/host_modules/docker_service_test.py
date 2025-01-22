import errno
import docker
import json
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
            rc, _ = docker_service.stop("syncd")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.stop.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_stop_fail_disallowed(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.stop("bad-container")

        assert rc == errno.EPERM, "Return code is wrong"
        assert (
            "not" in msg and "allowed" in msg
        ), "Message should contain 'not' and 'allowed'"

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
            rc, msg = docker_service.stop("syncd")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")

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
            rc, msg = docker_service.stop("syncd")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.stop.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_kill_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.kill.return_value = None

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.kill("syncd")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.kill.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_kill_fail_disallowed(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.kill("bad-container")

        assert rc == errno.EPERM, "Return code is wrong"
        assert (
            "not" in msg and "allowed" in msg
        ), "Message should contain 'not' and 'allowed'"

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
            rc, msg = docker_service.kill("syncd")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")

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
            rc, msg = docker_service.kill("syncd")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.kill.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_restart_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.get.return_value.restart.return_value = None

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.restart("syncd")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.restart.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_restart_fail_disallowed(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.restart("bad-container")

        assert rc == errno.EPERM, "Return code is wrong"
        assert (
            "not" in msg and "allowed" in msg
        ), "Message should contain 'not' and 'allowed'"

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
            rc, msg = docker_service.restart("syncd")

        assert rc == errno.ENOENT, "Return code is wrong"
        assert (
            "not" in msg and "exist" in msg
        ), "Message should contain 'not' and 'exist'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")

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
            rc, msg = docker_service.restart("syncd")

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.get.assert_called_once_with("syncd")
        mock_docker_client.containers.get.return_value.restart.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_run_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.run.return_value.name = "syncd"

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.run("docker-syncd-brcm:latest", "", {})

        assert rc == 0, "Return code is wrong"
        mock_docker_client.containers.run.assert_called_once_with(
            "docker-syncd-brcm:latest", "", **{}
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_run_fail_image_not_found(
        self, MockInit, MockBusName, MockSystemBus
    ):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.run.side_effect = docker.errors.ImageNotFound(
            "Image not found"
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.run("docker-syncd-brcm:latest", "", {})

        assert rc == errno.ENOENT, "Return code is wrong"
        assert "not found" in msg, "Message should contain 'not found'"
        mock_docker_client.containers.run.assert_called_once_with(
            "docker-syncd-brcm:latest", "", **{}
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_run_fail_api_error(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.containers.run.side_effect = docker.errors.APIError(
            "API error"
        )

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.run("docker-syncd-brcm:latest", "", {})

        assert rc != 0, "Return code is wrong"
        assert "API error" in msg, "Message should contain 'API error'"
        mock_docker_client.containers.run.assert_called_once_with(
            "docker-syncd-brcm:latest", "", **{}
        )

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_run_fail_image_not_allowed(
        self, MockInit, MockBusName, MockSystemBus
    ):
        mock_docker_client = mock.Mock()
        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.run("wrong_image_name", "", {})
        assert rc == errno.EPERM, "Return code is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_run_fail_non_empty_command(
        self, MockInit, MockBusName, MockSystemBus
    ):
        mock_docker_client = mock.Mock()
        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, msg = docker_service.run("docker-syncd-brcm:latest", "rm -rf /", {})
        assert rc == errno.EPERM, "Return code is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_load_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_docker_client.images.load.return_value = ["loaded_image"]

        with mock.patch("builtins.open", mock.mock_open(read_data="data")) as MockOpen, \
             mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.load("image.tar")

        assert rc == 0, "Return code is wrong"
        mock_docker_client.images.load.assert_called_once_with(MockOpen.return_value)
        MockOpen.assert_called_once_with("image.tar", "rb")

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_load_file_not_found(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()

        with mock.patch("builtins.open", mock.mock_open()) as MockOpen, \
             mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            MockOpen.side_effect = FileNotFoundError
            docker_service = DockerService(MOD_NAME)
            rc, _ = docker_service.load("non_existent_image.tar")

        assert rc == errno.ENOENT, "Return code is wrong"
        MockOpen.assert_called_once_with("non_existent_image.tar", "rb")

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_docker_list_success(self, MockInit, MockBusName, MockSystemBus):
        mock_docker_client = mock.Mock()
        mock_container_1 = mock.Mock(id="1", status="running", image=mock.Mock(tags=["image1"], id="hash1"), labels={})
        mock_container_2 = mock.Mock(id="2", status="exited", image=mock.Mock(tags=["image2"], id="hash2"), labels={})
        # The name attribute needs to be explicitly set for the mock object.
        mock_container_1.name = "container1"
        mock_container_2.name = "container2"
        mock_docker_client.containers.list.return_value = [
            mock_container_1, mock_container_2
        ]

        with mock.patch.object(docker, "from_env", return_value=mock_docker_client):
            docker_service = DockerService(MOD_NAME)
            rc, containers = docker_service.list(True, {})

        assert rc == 0, "Return code is wrong {}".format(containers)
        expected_containers = [
            {
                "id": "1",
                "name": "container1",
                "status": "running",
                "image": "image1",
                "labels": {},
                "hash": "hash1",
            },
            {
                "id": "2",
                "name": "container2",
                "status": "exited",
                "image": "image2",
                "labels": {},
                "hash": "hash2",
            },
        ]
        assert json.loads(containers) == expected_containers, "Containers list is wrong"
        mock_docker_client.containers.list.assert_called_once_with(all=True, filters={})