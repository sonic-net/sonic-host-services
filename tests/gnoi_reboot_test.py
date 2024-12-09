"""Tests for gnoi_reboot."""

import imp
import sys
import os
import pytest
import datetime

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    # Expect the 'mock' package for python 2
    # https://pypi.python.org/pypi/mock
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

TEST_ACTIVE_RESPONSE_DATA = "{\"active\": true, \"when\": 1617811205, \"reason\": \"testing reboot response\"}"
TEST_INACTIVE_RESPONSE_DATA = "{\"active\": false, \"when\": 0, \"reason\": \"\"}"

REBOOTMETHOD_UNKNOWN_ENUM = 0
REBOOTMETHOD_COLD_BOOT_ENUM = 1
REBOOTMETHOD_WARM_BOOT_ENUM = 4
#REBOOTMETHOD_NSF_ENUM = 6 

TEST_TIMESTAMP = 1618942253.831912040
REPORT_CRITICAL_STATE_FULL_COMMAND = "redis-cli -n 6 HSET COMPONENT_STATE_TABLE|host state ERROR reason \"cold reboot has failed\" essential true timestamp \"2021-04-20 18:10:53\" timestamp-seconds 1618942253 timestamp-nanoseconds 831912040"

VALID_REBOOT_REQUEST_COLD = "{\"method\": 1, \"message\": \"test reboot request reason\"}"
VALID_REBOOT_REQUEST_WARM = "{\"method\": \"WARM\", \"message\": \"test reboot request reason\"}"
INVALID_REBOOT_REQUEST = "\"method\": 1, \"message\": \"test reboot request reason\""

imp.load_source("host_service", host_modules_path + "/host_service.py")
imp.load_source("gnoi_reboot", host_modules_path + "/gnoi_reboot.py")
from gnoi_reboot import *


class TestGnoiReboot(object):
    @classmethod
    def setup_class(cls):
        with mock.patch("gnoi_reboot.super") as mock_host_module:
            cls.gnoi_reboot_module = GnoiReboot(MOD_NAME)

    def test_populate_reboot_status_flag(self):
        with mock.patch("time.time", return_value=1617811205.25):
            self.gnoi_reboot_module.populate_reboot_status_flag()
            assert self.gnoi_reboot_module.reboot_status_flag["active"] == False
            assert self.gnoi_reboot_module.reboot_status_flag["when"] == 0
            assert self.gnoi_reboot_module.reboot_status_flag["reason"] == ""

    def test_validate_reboot_request_success_cold_boot_enum_method(self):
        reboot_request = {"method": REBOOTMETHOD_COLD_BOOT_ENUM, "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_cold_boot_string_method(self):
        reboot_request = {"method": "COLD", "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_warm_enum_method(self):
        reboot_request = {"method": REBOOTMETHOD_WARM_BOOT_ENUM, "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_WARM_enum_method(self):
        reboot_request = {"method": "WARM", "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_fail_unknown_method(self):
        reboot_request = {"method": 0, "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Invalid reboot method: 0"

    def test_validate_reboot_request_fail_no_method(self):
        reboot_request = {"reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Reboot request must contain a reboot method"

    def test_validate_reboot_request_fail_delayed_reboot(self):
        reboot_request = {"method": REBOOTMETHOD_COLD_BOOT_ENUM, "delay": 10, "reason": "test reboot request reason"}
        result = self.gnoi_reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Delayed reboot is not supported"

    def test_execute_reboot_success(self):
        with (
            mock.patch("gnoi_reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("gnoi_reboot.GnoiReboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
        ):
            mock_run_command.return_value = (0, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.gnoi_reboot_module.execute_reboot("WARM")
            mock_run_command.assert_called_once_with("/usr/local/bin/warm-reboot -v")
            mock_sleep.assert_called_once_with(260)
            mock_populate_reboot_status_flag.assert_called_once_with()

    def test_execute_reboot_fail_unknown_reboot(self, caplog):
        with caplog.at_level(logging.ERROR):
            self.gnoi_reboot_module.execute_reboot(-1)
            msg = "gnoi_reboot: Invalid reboot method: -1"
            assert caplog.records[0].message == msg

    def test_execute_reboot_fail_issue_reboot_command_cold_boot(self, caplog):
        with (
            mock.patch("gnoi_reboot._run_command") as mock_run_command,
            mock.patch("gnoi_reboot.GnoiReboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute cold reboot"], ["stderror: execute cold reboot"])
            self.gnoi_reboot_module.execute_reboot(REBOOTMETHOD_COLD_BOOT_ENUM)
            msg = ("gnoi_reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute cold reboot'], stderr: "
                    "['stderror: execute cold reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with()

    def test_execute_reboot_fail_issue_reboot_command_warm(self, caplog):
        with (
            mock.patch("gnoi_reboot._run_command") as mock_run_command,
            mock.patch("gnoi_reboot.GnoiReboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.gnoi_reboot_module.execute_reboot("WARM")
            msg = ("gnoi_reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute WARM reboot'], stderr: "
                    "['stderror: execute WARM reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with()

    def test_issue_reboot_success_cold_boot(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("gnoi_reboot.GnoiReboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.gnoi_reboot_module.populate_reboot_status_flag()
            result = self.gnoi_reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.gnoi_reboot_module.execute_reboot,
                args=(REBOOTMETHOD_COLD_BOOT_ENUM,),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_success_warm(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("gnoi_reboot.GnoiReboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.gnoi_reboot_module.populate_reboot_status_flag()
            result = self.gnoi_reboot_module.issue_reboot([VALID_REBOOT_REQUEST_WARM])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.gnoi_reboot_module.execute_reboot,
                args=("WARM",),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_previous_reboot_ongoing(self):
        self.gnoi_reboot_module.populate_reboot_status_flag()
        self.gnoi_reboot_module.reboot_status_flag["active"] = True
        result = self.gnoi_reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
        assert result[0] == 1
        assert result[1] == "Previous reboot is ongoing"

    def test_issue_reboot_bad_format_reboot_request(self):
        self.gnoi_reboot_module.populate_reboot_status_flag()
        result = self.gnoi_reboot_module.issue_reboot([INVALID_REBOOT_REQUEST])
        assert result[0] == 1
        assert result[1] == "Failed to parse json formatted reboot request into python dict"

    def test_issue_reboot_invalid_reboot_request(self):
        with mock.patch("gnoi_reboot.GnoiReboot.validate_reboot_request", return_value=(1, "failed to validate reboot request")):
            self.gnoi_reboot_module.populate_reboot_status_flag()
            result = self.gnoi_reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 1
            assert result[1] == "failed to validate reboot request"

    def raise_runtime_exception_test(self):
        raise RuntimeError('test raise RuntimeError exception')

    def test_issue_reboot_fail_issue_reboot_thread(self):
        with mock.patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = self.raise_runtime_exception_test
            self.gnoi_reboot_module.populate_reboot_status_flag()
            result = self.gnoi_reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 1
            assert result[1] == "Failed to start thread to execute reboot with error: test raise RuntimeError exception"

    def test_get_reboot_status_active(self):
        self.gnoi_reboot_module.populate_reboot_status_flag(True, 1617811205, "testing reboot response")
        result = self.gnoi_reboot_module.get_reboot_status()
        assert result[0] == 0
        assert result[1] == TEST_ACTIVE_RESPONSE_DATA

    def test_get_reboot_status_inactive(self):
        self.gnoi_reboot_module.populate_reboot_status_flag(False, 0, "")
        result = self.gnoi_reboot_module.get_reboot_status()
        assert result[0] == 0
        assert result[1] == TEST_INACTIVE_RESPONSE_DATA

    def test_register(self):
        result = register()
        assert result[0] == GnoiReboot
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
