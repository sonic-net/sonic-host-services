import sys
import os
import pytest
from unittest import mock
from host_modules import config_engine

class TestConfigEngine(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_reload(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_db_json = "{}"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.reload(config_db_json, "gnmi")
            call_args_list = mock_run.call_args_list
            args, _ = call_args_list[0]
            assert ["/usr/bin/systemctl", "mask", "gnmi"] in args
            args, _ = call_args_list[1]
            assert ["/usr/local/bin/config", "reload", "-y", "/dev/stdin"] in args
            args, _ = call_args_list[2]
            assert ["/usr/bin/systemctl", "unmask", "gnmi"] in args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_db_json = "{}"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.reload(config_db_json, "gnmi")
            call_args_list = mock_run.call_args_list
            args, _ = call_args_list[0]
            assert ["/usr/bin/systemctl", "mask", "gnmi"] in args
            args, _ = call_args_list[1]
            assert ["/usr/local/bin/config", "reload", "-y", "/dev/stdin"] in args
            args, _ = call_args_list[2]
            assert ["/usr/bin/systemctl", "unmask", "gnmi"] in args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_reload_force(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_db_json = "{}"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.reload_force(config_db_json, "gnmi")
            call_args_list = mock_run.call_args_list
            args, _ = call_args_list[0]
            assert ["/usr/bin/systemctl", "mask", "gnmi"] in args
            args, _ = call_args_list[1]
            assert ["/usr/local/bin/config", "reload", "-y", "-f", "/dev/stdin"] in args
            args, _ = call_args_list[2]
            assert ["/usr/bin/systemctl", "unmask", "gnmi"] in args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_db_json = "{}"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.reload_force(config_db_json, "gnmi")
            call_args_list = mock_run.call_args_list
            args, _ = call_args_list[0]
            assert ["/usr/bin/systemctl", "mask", "gnmi"] in args
            args, _ = call_args_list[1]
            assert ["/usr/local/bin/config", "reload", "-y", "-f", "/dev/stdin"] in args
            args, _ = call_args_list[2]
            assert ["/usr/bin/systemctl", "unmask", "gnmi"] in args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_save(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_file = "test.patch"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.save(config_file)
            call_args = mock_run.call_args[0][0]
            assert "save" in call_args
            assert config_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            config_file = "test.patch"
            config_stub = config_engine.Config(config_engine.MOD_NAME)
            ret, msg = config_stub.save(config_file)
            call_args = mock_run.call_args[0][0]
            assert "save" in call_args
            assert config_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

