import sys
import os
import pytest
from unittest import mock
from host_modules import showtech

class TestShowtech(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_info(self, MockInit, MockBusName, MockSystemBus):
        with mock.patch("subprocess.run") as mock_run:
            res_mock = mock.Mock()
            test_ret = 0
            exp_msg = "/var/abcdumpdef.gz"
            test_msg = "######" + exp_msg + "-------"
            date_msg = "yesterday once more"
            attrs = {"returncode": test_ret, "stdout": test_msg}
            res_mock.configure_mock(**attrs)
            mock_run.return_value = res_mock
            patch_file = "test.patch"
            showtech_stub = showtech.Showtech(showtech.MOD_NAME)
            ret, msg = showtech_stub.info(date_msg)
            call_args = mock_run.call_args[0][0]
            assert "/usr/local/bin/generate_dump" in call_args
            assert date_msg in call_args
            assert ret == test_ret, "Return value is wrong"
            assert msg == exp_msg, "Return message is wrong"
        