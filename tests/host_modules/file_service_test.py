import errno
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
    @mock.patch("os.remove")
    def test_file_remove(self, mock_remove, MockInit, MockBusName, MockSystemBus):
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "test_path"
        ret, msg = file_service_stub.remove(path)

        mock_remove.assert_called_with(path)
        assert ret == 0
        assert msg == ""

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    def test_file_remove_error(self, mock_remove, MockInit, MockBusName, MockSystemBus):
        err = errno.ENOENT
        path = "test_path"
        mock_remove.side_effect = FileNotFoundError(err, os.strerror(err), path)

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.remove(path)

        assert ret == err
        assert msg == f"[Errno {err}] {os.strerror(err)}: '{path}'"