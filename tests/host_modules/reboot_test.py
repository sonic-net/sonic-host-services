"""Tests for reboot."""

import importlib.util
import importlib.machinery
import json
import sys
import os
import time
import pytest
import logging

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    # Expect the 'mock' package for python 2
    # https://pypi.python.org/pypi/mock
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "../host_modules")
sys.path.insert(0, sonic_host_service_path)

from host_modules.reboot import RebootStatus

TIME = 1617811205
TEST_ACTIVE_RESPONSE_DATA = "{\"active\": true, \"when\": 1617811205, \"reason\": \"testing reboot response\"}"
TEST_INACTIVE_RESPONSE_DATA = "{\"active\": false, \"when\": 0, \"reason\": \"\"}"

REBOOT_METHOD_UNKNOWN_ENUM = 0
REBOOT_METHOD_COLD_BOOT_ENUM = 1
REBOOT_METHOD_HALT_BOOT_ENUM = 3
REBOOT_METHOD_WARM_BOOT_ENUM = 4

HALT_TIMEOUT = 60

TEST_TIMESTAMP = 1618942253.831912040

VALID_REBOOT_REQUEST_COLD = "{\"method\": 1, \"message\": \"test reboot request reason\"}"
VALID_REBOOT_REQUEST_HALT = "{\"method\": 3, \"message\": \"test reboot request reason\"}"
VALID_REBOOT_REQUEST_WARM = "{\"method\": \"WARM\", \"message\": \"test reboot request reason\"}"
INVALID_REBOOT_REQUEST = "\"method\": 1, \"message\": \"test reboot request reason\""

def load_source(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, filename)
    spec = importlib.util.spec_from_file_location(modname, filename, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # The module is always executed and not cached in sys.modules.
    # Uncomment the following line to cache the module.
    sys.modules[module.__name__] = module
    loader.exec_module(module)
    return module

load_source("host_service", host_modules_path + "/host_service.py")
load_source("reboot", host_modules_path + "/reboot.py")
from reboot import *


class TestReboot(object):
    @classmethod
    def setup_class(cls):
        with mock.patch("reboot.super") as mock_host_module:
            cls.reboot_module = Reboot(MOD_NAME)

    def test_populate_reboot_status_flag(self):
        with mock.patch("time.time", return_value=1617811205.25):
            self.reboot_module.populate_reboot_status_flag()
            return_value, get_reboot_status_flag_data = self.reboot_module.get_reboot_status()
            assert return_value == 0
            get_reboot_status_flag_data = json.loads(get_reboot_status_flag_data)
            assert get_reboot_status_flag_data["active"] == False
            assert get_reboot_status_flag_data["when"] == 0
            assert get_reboot_status_flag_data["reason"] == ""
            assert get_reboot_status_flag_data["count"] == 0
            assert get_reboot_status_flag_data["method"] == ""
            assert get_reboot_status_flag_data["status"] == {
                "status": RebootStatus.STATUS_UNKNOWN.value,
                "message": ""
            }

    def test_populate_reboot_status_flag_with_status(self):
        with mock.patch("time.time", return_value=1617811205.25):
            self.reboot_module.populate_reboot_status_flag(status=RebootStatus.STATUS_SUCCESS)
            return_value, get_reboot_status_flag_data = self.reboot_module.get_reboot_status()
            assert return_value == 0
            get_reboot_status_flag_data = json.loads(get_reboot_status_flag_data)
            assert get_reboot_status_flag_data["status"] == {
                "status": RebootStatus.STATUS_SUCCESS.value,
                "message": ""
            }

    def test_validate_reboot_request_success_cold_boot_enum_method(self):
        reboot_request = {"method": REBOOT_METHOD_COLD_BOOT_ENUM, "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_cold_boot_string_method(self):
        reboot_request = {"method": "COLD", "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_halt_boot_enum_method(self):
        reboot_request = {"method": REBOOT_METHOD_HALT_BOOT_ENUM, "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_halt_boot_string_method(self):
        reboot_request = {"method": "HALT", "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_warm_enum_method(self):
        reboot_request = {"method": REBOOT_METHOD_WARM_BOOT_ENUM, "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_success_WARM_enum_method(self):
        reboot_request = {"method": "WARM", "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 0
        assert result[1] == ""

    def test_validate_reboot_request_fail_unknown_method(self):
        reboot_request = {"method": 0, "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Unsupported reboot method: 0"

    def test_validate_reboot_request_fail_no_method(self):
        reboot_request = {"reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Reboot request must contain a reboot method"

    def test_validate_reboot_request_fail_delayed_reboot(self):
        reboot_request = {"method": REBOOT_METHOD_COLD_BOOT_ENUM, "delay": 10, "reason": "test reboot request reason"}
        result = self.reboot_module.validate_reboot_request(reboot_request)
        assert result[0] == 1
        assert result[1] == "Delayed reboot is not supported"

    def test_is_container_running_success(self):
        with mock.patch("docker.from_env") as mock_docker:
            mock_client = mock.Mock()
            mock_docker.return_value = mock_client
            mock_container = mock.Mock()
            mock_container.name = "pmon"
            mock_container.attrs = {'State': {'Running': True}}
            mock_client.containers.list.return_value = [mock_container]

            result = self.reboot_module.is_container_running("pmon")
            assert result is True
            mock_client.containers.list.assert_called_once_with(filters={"name": "^pmon$"})

    def test_is_container_running_failure(self):
        with mock.patch("docker.from_env") as mock_docker:
            mock_client = mock.Mock()
            mock_docker.return_value = mock_client
            mock_client.containers.list.return_value = []

            result = self.reboot_module.is_container_running("pmon")
            assert result is False
            mock_client.containers.list.assert_called_once_with(filters={"name": "^pmon$"})

    def test_is_container_running_exception(self, caplog):
        with mock.patch("docker.from_env", side_effect=Exception("Docker error")) as mock_docker, \
             caplog.at_level(logging.ERROR):
            result = self.reboot_module.is_container_running("pmon")
            assert result is False
            assert any("Error checking container status for pmon: [Docker error]" in record.message for record in caplog.records)

    def test_is_halt_command_running_success(self):
        with mock.patch("psutil.process_iter") as mock_process_iter:
            mock_process = mock.Mock()
            mock_process.info = {'cmdline': ['reboot', '-p']}
            mock_process_iter.return_value = [mock_process]

            result = self.reboot_module.is_halt_command_running()
            assert result is True
            mock_process_iter.assert_called_once_with(['cmdline'])

    def test_is_halt_command_running_failure(self):
        with mock.patch("psutil.process_iter") as mock_process_iter:
            mock_process = mock.Mock()
            mock_process.info = {'cmdline': ['other_process']}
            mock_process_iter.return_value = [mock_process]

            result = self.reboot_module.is_halt_command_running()
            assert result is False
            mock_process_iter.assert_called_once_with(['cmdline'])

    def test_is_halt_command_running_exception(self, caplog):
        with mock.patch("psutil.process_iter", side_effect=Exception("psutil error")) as mock_process_iter, \
             caplog.at_level(logging.ERROR):
            result = self.reboot_module.is_halt_command_running()
            assert result is False
            assert any("Error checking if halt command is running: [psutil error]" in record.message for record in caplog.records)

    def test_execute_reboot_success(self):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
        ):
            mock_run_command.return_value = (0, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.reboot_module.execute_reboot("WARM")
            mock_run_command.assert_called_once_with("sudo warm-reboot")
            mock_sleep.assert_called_once_with(260)
            mock_populate_reboot_status_flag.assert_called_once_with(False, int(time.time()), "Reboot command failed to execute", 'WARM', RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_fail_unknown_reboot(self, caplog):
        with caplog.at_level(logging.ERROR):
            self.reboot_module.execute_reboot(-1)
            msg = "reboot: Unsupported reboot method: -1"
            assert caplog.records[0].message == msg

    def test_execute_reboot_fail_issue_reboot_command_cold_boot(self, caplog):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute cold reboot"], ["stderror: execute cold reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_COLD_BOOT_ENUM)
            msg = ("reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute cold reboot'], stderr: "
                    "['stderror: execute cold reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, int (time.time()), "Failed to execute reboot command", 1, RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_fail_issue_reboot_command_halt(self, caplog):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute halt reboot"], ["stderror: execute halt reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM)
            msg = ("reboot: Reboot failed execution with "
                   "stdout: ['stdout: execute halt reboot'], stderr: "
                   "['stderror: execute halt reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, int (time.time()), "Failed to execute reboot command", 3, RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_success_halt(self):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False) as mock_is_halt_command_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=False) as mock_is_container_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
        ):
            mock_run_command.return_value = (0, ["stdout: execute halt reboot"], ["stderror: execute halt reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM)
            mock_run_command.assert_called_once_with("sudo reboot -p")
            mock_is_halt_command_running.assert_called()
            mock_is_container_running.assert_called_with("pmon")
            mock_populate_reboot_status_flag.assert_called_once_with(False, 0, 'Halt reboot completed', 3, RebootStatus.STATUS_SUCCESS)

    def test_execute_reboot_fail_halt_timeout(self, caplog):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=True) as mock_is_halt_command_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=True) as mock_is_container_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (0, ["stdout: execute halt reboot"], ["stderror: execute halt reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM)
            mock_run_command.assert_called_once_with("sudo reboot -p")
            mock_sleep.assert_called_with(5)
            mock_is_halt_command_running.assert_called()
            assert any("HALT reboot failed: Services are still running" in record.message for record in caplog.records)
            mock_populate_reboot_status_flag.assert_called_once_with(False, int(time.time()), 'Halt reboot did not complete', 3, RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_fail_issue_reboot_command_warm(self, caplog):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.reboot_module.execute_reboot("WARM")
            msg = ("reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute WARM reboot'], stderr: "
                    "['stderror: execute WARM reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, int (time.time()), "Failed to execute reboot command", 'WARM', RebootStatus.STATUS_FAILURE)

    def test_issue_reboot_success_cold_boot(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.reboot_module.execute_reboot,
                args=(REBOOT_METHOD_COLD_BOOT_ENUM,),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_success_cold_no_message(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
        ):
            request = json.loads(VALID_REBOOT_REQUEST_COLD)
            del request["message"]
            request_str = json.dumps(request)

            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([request_str])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.reboot_module.execute_reboot,
                args=(REBOOT_METHOD_COLD_BOOT_ENUM,),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_success_halt(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_HALT])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.reboot_module.execute_reboot,
                args=(REBOOT_METHOD_HALT_BOOT_ENUM,),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_success_warm(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_WARM])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_thread.assert_called_once_with(
                target=self.reboot_module.execute_reboot,
                args=("WARM",),
            )
            mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reboot_previous_reboot_ongoing(self):
        self.reboot_module.populate_reboot_status_flag()
        self.reboot_module.reboot_status_flag["active"] = True
        result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
        assert result[0] == 1
        assert result[1] == "Previous reboot is ongoing"

    def test_issue_reboot_bad_format_reboot_request(self):
        self.reboot_module.populate_reboot_status_flag()
        result = self.reboot_module.issue_reboot([INVALID_REBOOT_REQUEST])
        assert result[0] == 1
        assert result[1] == "Failed to parse json formatted reboot request into python dict"

    def test_issue_reboot_invalid_reboot_request(self):
        with mock.patch("reboot.Reboot.validate_reboot_request", return_value=(1, "failed to validate reboot request")):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 1
            assert result[1] == "failed to validate reboot request"

    def raise_runtime_exception_test(self):
        raise RuntimeError('test raise RuntimeError exception')

    def test_issue_reboot_fail_issue_reboot_thread(self):
        with mock.patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = self.raise_runtime_exception_test
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 1
            assert result[1] == "Failed to start thread to execute reboot with error: test raise RuntimeError exception"

    def test_get_reboot_status_active(self):
        MSG="testing reboot response"
        self.reboot_module.populate_reboot_status_flag(True, TIME, MSG, REBOOT_METHOD_COLD_BOOT_ENUM, RebootStatus.STATUS_SUCCESS)
        result = self.reboot_module.get_reboot_status()
        assert result[0] == 0
        response_data = json.loads(result[1])
        assert response_data["active"] == True
        assert response_data["when"] == TIME
        assert response_data["reason"] == MSG
        assert response_data["method"] == REBOOT_METHOD_COLD_BOOT_ENUM
        assert response_data["status"] == {
            "status": RebootStatus.STATUS_SUCCESS.value,
            "message": ""
        }

    def test_get_reboot_status_inactive(self):
        self.reboot_module.populate_reboot_status_flag(False, 0, "", REBOOT_METHOD_COLD_BOOT_ENUM, RebootStatus.STATUS_SUCCESS)
        result = self.reboot_module.get_reboot_status()
        assert result[0] == 0
        response_data = json.loads(result[1])
        assert response_data["active"] == False
        assert response_data["when"] == 0
        assert response_data["reason"] == ""
        assert response_data["method"] == REBOOT_METHOD_COLD_BOOT_ENUM
        assert response_data["status"] == {
            "status": RebootStatus.STATUS_SUCCESS.value,
            "message": ""
        }

#        assert result[1] == TEST_INACTIVE_RESPONSE_DATA

    def test_register(self):
        result = register()
        assert result[0] == Reboot
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
