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
    def test_download_success(self, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        """
        Test the download method for a successful file download.
        """
        # Mock the SSH client and its methods
        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_sftp = mock.Mock()
        mock_ssh.open_sftp.return_value = mock_sftp

        # Create a FileService instance
        file_service_stub = file_service.FileService(file_service.MOD_NAME)

        # Call the download method
        hostname = "example.com"
        username = "user"
        password = "password"
        remote_path = "/remote/path/file.txt"
        local_path = "/local/path/file.txt"
        ret = file_service_stub.download(hostname, username, password, remote_path, local_path)

        # Assertions
        assert ret == 0
        mock_ssh.connect.assert_called_once_with(hostname, username=username, password=password)
        mock_sftp.get.assert_called_once_with(remote_path, local_path)
        mock_sftp.close.assert_called_once()
        mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    def test_download_failure(self, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        """
        Test the download method for a failure during file download.
        """
        # Mock the SSH client and its methods
        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_ssh.open_sftp.side_effect = Exception("SFTP error")

        # Create a FileService instance
        file_service_stub = file_service.FileService(file_service.MOD_NAME)

        # Call the download method
        hostname = "example.com"
        username = "user"
        password = "password"
        remote_path = "/remote/path/file.txt"
        local_path = "/local/path/file.txt"
        ret, msg = file_service_stub.download(hostname, username, password, remote_path, local_path)

        # Assertions
        assert ret == 1
        assert "error" in msg
        assert "SFTP error" in msg["error"]
        mock_ssh.connect.assert_called_once_with(hostname, username=username, password=password)
        mock_ssh.close.assert_called_once()