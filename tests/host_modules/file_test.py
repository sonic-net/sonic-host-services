import sys
import os
import pytest
from unittest import mock
from host_modules import file_service

class TestFileService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    @mock.patch("os.umask")
    def test_get_file_stat_valid(self, mock_umask, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_stat_result = mock.Mock()
        mock_stat_result.st_mtime = 1609459200.0  # 2021-01-01 00:00:00 in nanoseconds
        mock_stat_result.st_mode = 0o100644  # Regular file with permissions
        mock_stat_result.st_size = 1024
        mock_stat.return_value = mock_stat_result

        mock_umask.return_value = 0o022  # Default umask

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/valid/path"
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 0
        assert msg['path'] == path
        assert msg['last_modified'] == "1609459200000000000"
        assert msg['permissions'] == "644"
        assert msg['size'] == "1024"
        assert msg['umask'] == "o22"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    def test_get_file_stat_invalid_path(self, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_stat.side_effect = FileNotFoundError("[Errno 2] No such file or directory")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/invalid/path"
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 1
        assert 'error' in msg
        assert "No such file or directory" in msg['error']

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_get_file_stat_empty_path(self, MockInit, MockBusName, MockSystemBus):
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = ""
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 1
        assert "Dbus get_file_stat called with no path specified" in msg['error']

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_sftp_success(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_sftp = mock.Mock()
        mock_ssh.open_sftp.return_value = mock_sftp

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="SFTP"
        )

        assert ret == 0
        assert msg == ""
        mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
        mock_sftp.get.assert_called_once_with("/remote/path/file.txt", "/local/path/file.txt")
        mock_sftp.close.assert_called_once()
        mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_sftp_failure(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_ssh.open_sftp.side_effect = Exception("SFTP error")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="SFTP"
        )

        assert ret == 1
        assert "SFTP error" in msg
        mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
        mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("requests.get")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_success(self, mock_exists, mock_stat, MockRequestsGet, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_response = mock.Mock()
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]
        mock_response.raise_for_status.return_value = None
        MockRequestsGet.return_value = mock_response

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        with mock.patch("builtins.open", mock.mock_open()) as mock_file:
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="http://example.com/file.txt",
                local_path="/local/path/file.txt",
                protocol="HTTP"
            )

            assert ret == 0
            assert msg == ""
            MockRequestsGet.assert_called_once_with(
                "http://example.com/file.txt", auth=("user", "password"), stream=True
            )
            mock_file().write.assert_any_call(b"chunk1")
            mock_file().write.assert_any_call(b"chunk2")

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("requests.get")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_failure(self, mock_exists, mock_stat, MockRequestsGet, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        MockRequestsGet.side_effect = Exception("HTTP error")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="http://example.com/file.txt",
            local_path="/local/path/file.txt",
            protocol="HTTP"
        )

        assert ret == 1
        assert "HTTP error" in msg

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_scp_success(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_scp = mock.Mock()
        with mock.patch("scp.SCPClient", return_value=mock_scp) as MockSCPClient:
            file_service_stub = file_service.FileService(file_service.MOD_NAME)
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="/remote/path/file.txt",
                local_path="/local/path/file.txt",
                protocol="SCP"
            )

            assert ret == 0
            assert msg == ""
            mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
            MockSCPClient.assert_called_once_with(mock_ssh.get_transport())
            mock_scp.get.assert_called_once_with("/remote/path/file.txt", "/local/path/file.txt")
            mock_scp.close.assert_called_once()
            mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_scp_failure(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        with mock.patch("scp.SCPClient", side_effect=Exception("SCP error")):
            file_service_stub = file_service.FileService(file_service.MOD_NAME)
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="/remote/path/file.txt",
                local_path="/local/path/file.txt",
                protocol="SCP"
            )

            assert ret == 1
            assert "SCP error" in msg
            mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
            mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_unsupported_protocol(self, mock_exists, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="FTP"  # Unsupported protocol
        )

        assert ret == 1
        assert "Unsupported protocol" in msg

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_success(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable directory
        mock_stat.return_value = mock_dir_stat
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 0
        assert msg == ""
        mock_remove.assert_called_once_with(path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_file_not_found(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/nonexistent/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "File not found" in msg
        mock_remove.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_dir_not_world_writable(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40755  # Not world writable directory
        mock_stat.return_value = mock_dir_stat
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "Directory is not world writable" in msg
        mock_remove.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_dir_not_found(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_stat.side_effect = FileNotFoundError("No such directory")
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "Directory not found" in msg
        mock_remove.assert_not_called()