import sys
import os
import pytest
from unittest import mock
from host_modules import systemd_service

class TestSystemdService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_service_restart(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            service = "snmp"
            systemd_service_stub = systemd_service.SystemdService(systemd_service.MOD_NAME)
            ret, msg = systemd_service_stub.restart_service(service)
            call_args = mock_run.call_args[0][0]
            assert service in call_args
            assert "/usr/bin/systemctl" in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"

        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Dbus does not support"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            service = "unsupported"
            systemd_service_stub = systemd_service.SystemdService(systemd_service.MOD_NAME)
            ret, msg = systemd_service_stub.restart_service(service)
            call_args = mock_run.call_args[0][0]
            assert "/usr/bin/systemctl" in call_args
            assert service in call_args
            assert "stop" in call_args
            assert ret == test_ret, "Return value is wrong"
            assert "Dbus does not support" in msg, "Return message is wrong"
    
        
    def test_service_stop(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            service = "snmp"
            systemd_service_stub = systemd_service.SystemdService(systemd_service.MOD_NAME)
            ret, msg = systemd_service_stub.stop_service(service)
            call_args = mock_run.call_args[0][0]
            assert service in call_args
            assert "/usr/bin/systemctl" in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"

        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Dbus does not support"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            service = "unsupported"
            systemd_service_stub = systemd_service.SystemdService(systemd_service.MOD_NAME)
            ret, msg = systemd_service_stub.stop_service(service)
            call_args = mock_run.call_args[0][0]
            assert "/usr/bin/systemctl" in call_args
            assert service in call_args
            assert "stop" in call_args
            assert ret == test_ret, "Return value is wrong"
            assert "Dbus does not support" in msg, "Return message is wrong"