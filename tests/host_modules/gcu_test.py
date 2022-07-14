import sys
import os
import pytest
from unittest import mock
from host_modules import gcu

class TestGCU(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_apply_patch_db(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            patch_file = "test.patch"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.apply_patch_db(patch_file)
            call_args = mock_run.call_args[0][0]
            assert "apply-patch" in call_args
            assert "CONFIGDB" in call_args
            assert patch_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            patch_file = "test.patch"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.apply_patch_db(patch_file)
            call_args = mock_run.call_args[0][0]
            assert "apply-patch" in call_args
            assert "CONFIGDB" in call_args
            assert patch_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_apply_patch_yang(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            patch_file = "test.patch"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.apply_patch_yang(patch_file)
            call_args = mock_run.call_args[0][0]
            assert "apply-patch" in call_args
            assert "SONICYANG" in call_args
            assert patch_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            patch_file = "test.patch"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.apply_patch_yang(patch_file)
            call_args = mock_run.call_args[0][0]
            assert "apply-patch" in call_args
            assert "SONICYANG" in call_args
            assert patch_file in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_create_checkpoint(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            cp_name = "test_name"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.create_checkpoint(cp_name)
            call_args = mock_run.call_args[0][0]
            assert "checkpoint" in call_args
            assert "delete-checkpoint" not in call_args
            assert cp_name in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            cp_name = "test_name"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.create_checkpoint(cp_name)
            call_args = mock_run.call_args[0][0]
            assert "checkpoint" in call_args
            assert "delete-checkpoint" not in call_args
            assert cp_name in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_delete_checkpoint(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            cp_name = "test_name"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.delete_checkpoint(cp_name)
            call_args = mock_run.call_args[0][0]
            assert "delete-checkpoint" in call_args
            assert cp_name in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "", "Return message is wrong"
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 1
            test_msg = b"Error: this is the test message\nHello world\n"
            attrs = {"returncode": test_ret, "stderr": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            cp_name = "test_name"
            gcu_stub = gcu.GCU(gcu.MOD_NAME)
            ret, msg = gcu_stub.delete_checkpoint(cp_name)
            call_args = mock_run.call_args[0][0]
            assert "delete-checkpoint" in call_args
            assert cp_name in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == "Error: this is the test message", "Return message is wrong"
        