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

import host_modules.reboot as host_reboot
from host_modules.reboot import RebootStatus

TIME = 1617811205
TEST_ACTIVE_RESPONSE_DATA = "{\"active\": true, \"when\": 1617811205, \"reason\": \"testing reboot response\"}"
TEST_INACTIVE_RESPONSE_DATA = "{\"active\": false, \"when\": 0, \"reason\": \"\"}"

REBOOT_METHOD_UNKNOWN_ENUM = 0
REBOOT_METHOD_COLD_BOOT_ENUM = 1
REBOOT_METHOD_HALT_BOOT_ENUM = 3
REBOOT_METHOD_WARM_BOOT_ENUM = 4

EXPECTED_HALT_TIMEOUT = 60

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

    def setup_method(self):
        self.reboot_module.active_request_message = ""

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

    @pytest.mark.parametrize(
        "result_string,status",
        [
            ("Halt reboot completed", RebootStatus.STATUS_SUCCESS),
            ("Halt reboot did not complete", RebootStatus.STATUS_FAILURE),
            ("Halt completion check could not be answered", RebootStatus.STATUS_FAILURE),
            ("Failed to execute reboot command", RebootStatus.STATUS_FAILURE),
            ("Reboot command failed to execute", RebootStatus.STATUS_FAILURE),
            ("Failed to write reboot cause", RebootStatus.STATUS_FAILURE),
        ],
    )
    def test_terminal_status_appends_original_request_message(self, result_string, status):
        request_message = "BMC pre-shutdown request [bmc-req:12345678-1234-4234-8234-123456789abc]"
        self.reboot_module.populate_reboot_status_flag(
            True, TIME, request_message, REBOOT_METHOD_HALT_BOOT_ENUM, RebootStatus.STATUS_UNKNOWN
        )
        self.reboot_module.populate_reboot_status_flag(
            False, TIME, result_string, REBOOT_METHOD_HALT_BOOT_ENUM, status
        )

        _, response = self.reboot_module.get_reboot_status()
        response_data = json.loads(response)

        assert response_data["reason"] == "{} | {}".format(result_string, request_message)
        assert response_data["status"] == {"status": status.value, "message": ""}
        assert set(response_data) == {"active", "when", "reason", "count", "method", "status"}

    def test_terminal_status_without_request_message_keeps_bare_reason(self):
        self.reboot_module.populate_reboot_status_flag(
            True, TIME, "", REBOOT_METHOD_COLD_BOOT_ENUM, RebootStatus.STATUS_UNKNOWN
        )
        self.reboot_module.populate_reboot_status_flag(
            False, TIME, "Failed to execute reboot command", REBOOT_METHOD_COLD_BOOT_ENUM,
            RebootStatus.STATUS_FAILURE
        )

        _, response = self.reboot_module.get_reboot_status()
        response_data = json.loads(response)

        assert response_data["reason"] == "Failed to execute reboot command"
        assert response_data["status"]["message"] == ""

    def test_get_dpu_halt_services_timeout_value(self):
        mock_data = {"dpu_halt_services_timeout": 120}

        with (
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=mock_data,
            ) as mock_platform_data,
            mock.patch("builtins.open") as mock_open,
        ):
            assert host_reboot.get_dpu_halt_services_timeout() == 120
            mock_platform_data.assert_called_once_with()
            mock_open.assert_not_called()

    def test_get_dpu_halt_services_timeout_none(self):
        mock_data = {"dpu_halt_services_timeout": None}

        with mock.patch(
            "host_modules.reboot.device_info.get_platform_json_data",
            return_value=mock_data,
        ):
            assert host_reboot.get_dpu_halt_services_timeout() == EXPECTED_HALT_TIMEOUT

    def test_get_dpu_halt_services_timeout_general_exception(self, caplog):
        with mock.patch(
            "host_modules.reboot.device_info.get_platform_json_data",
            side_effect=RuntimeError("unexpected reader failure"),
        ), caplog.at_level(logging.INFO):
            assert host_reboot.get_dpu_halt_services_timeout() == EXPECTED_HALT_TIMEOUT
        assert any("unexpected reader failure" in record.message for record in caplog.records)

    def test_get_dpu_halt_services_timeout_unavailable_is_logged(self, caplog):
        with mock.patch(
            "host_modules.reboot.device_info.get_platform_json_data",
            return_value=None,
        ), caplog.at_level(logging.INFO):
            assert (
                host_reboot.get_dpu_halt_services_timeout() ==
                EXPECTED_HALT_TIMEOUT
            )

        assert any(
            "platform.json data is unavailable or not a dictionary" in record.message
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        "platform_data,expected",
        [
            ({"switch_host_halt_services_timeout": 90}, 90),
            ({"switch_host_halt_services_timeout": True}, EXPECTED_HALT_TIMEOUT),
            ({"switch_host_halt_services_timeout": 1.5}, EXPECTED_HALT_TIMEOUT),
            ({"switch_host_halt_services_timeout": "90"}, EXPECTED_HALT_TIMEOUT),
            ({"switch_host_halt_services_timeout": None}, EXPECTED_HALT_TIMEOUT),
            ({"switch_host_halt_services_timeout": 0}, EXPECTED_HALT_TIMEOUT),
            ({"switch_host_halt_services_timeout": -1}, EXPECTED_HALT_TIMEOUT),
            ({}, EXPECTED_HALT_TIMEOUT),
        ],
        ids=[
            "positive-integer",
            "boolean",
            "float",
            "numeric-string",
            "null",
            "zero",
            "negative",
            "missing",
        ],
    )
    def test_switch_host_halt_services_timeout_requires_positive_integer(
            self, platform_data, expected):
        with (
            mock.patch(
                "host_modules.reboot.device_info.is_switch_host",
                return_value=True,
            ),
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=platform_data,
            ),
        ):
            assert host_reboot.get_halt_services_timeout() == expected

    @pytest.mark.parametrize(
        "is_switch_host,is_smartswitch,is_dpu,platform_data,expected",
        [
            (True, True, True, {"switch_host_halt_services_timeout": 90,
                                "dpu_halt_services_timeout": 120}, 90),
            (True, False, False, {"dpu_halt_services_timeout": 120}, EXPECTED_HALT_TIMEOUT),
            (True, False, False, {"switch_host_halt_services_timeout": 0,
                                  "dpu_halt_services_timeout": 120}, EXPECTED_HALT_TIMEOUT),
            (True, False, False, {"switch_host_halt_services_timeout": "invalid",
                                  "dpu_halt_services_timeout": 120}, EXPECTED_HALT_TIMEOUT),
            (False, True, False, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": 180}, 180),
            (False, True, False, {"switch_host_halt_services_timeout": 90}, EXPECTED_HALT_TIMEOUT),
            (False, True, False, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": "invalid"}, EXPECTED_HALT_TIMEOUT),
            (False, True, False, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": 0}, EXPECTED_HALT_TIMEOUT),
            (False, False, True, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": 120}, 120),
            (False, False, True, {"switch_host_halt_services_timeout": 90}, EXPECTED_HALT_TIMEOUT),
            (False, False, True, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": "invalid"}, EXPECTED_HALT_TIMEOUT),
            (False, False, True, {"switch_host_halt_services_timeout": 90,
                                  "dpu_halt_services_timeout": 0}, EXPECTED_HALT_TIMEOUT),
            (False, False, False, {"switch_host_halt_services_timeout": 90,
                                   "dpu_halt_services_timeout": 120}, EXPECTED_HALT_TIMEOUT),
        ],
        ids=[
            "switch-host-selects-switch-key",
            "switch-host-does-not-fall-through",
            "switch-host-zero-does-not-fall-through",
            "switch-host-invalid-does-not-fall-through",
            "smartswitch-npu-preserves-configured-value",
            "smartswitch-npu-does-not-fall-through",
            "smartswitch-npu-invalid-does-not-fall-through",
            "smartswitch-npu-zero-does-not-fall-through",
            "dpu-selects-dpu-key",
            "dpu-does-not-fall-through",
            "dpu-invalid-does-not-fall-through",
            "dpu-zero-does-not-fall-through",
            "other-identity-uses-default",
        ],
    )
    def test_get_halt_services_timeout_by_identity(
            self, is_switch_host, is_smartswitch, is_dpu,
            platform_data, expected):
        with (
            mock.patch(
                "host_modules.reboot.device_info.is_switch_host",
                return_value=is_switch_host,
            ) as mock_is_switch_host,
            mock.patch(
                "host_modules.reboot.device_info.is_smartswitch",
                return_value=is_smartswitch,
            ) as mock_is_smartswitch,
            mock.patch(
                "host_modules.reboot.device_info.is_dpu",
                return_value=is_dpu,
            ) as mock_is_dpu,
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=platform_data,
            ) as mock_platform_data,
            mock.patch("builtins.open") as mock_open,
        ):
            assert host_reboot.get_halt_services_timeout() == expected
            mock_open.assert_not_called()

        mock_is_switch_host.assert_called_once_with()
        if is_switch_host:
            mock_is_smartswitch.assert_not_called()
            mock_is_dpu.assert_not_called()
            mock_platform_data.assert_called_once_with()
        elif is_smartswitch:
            mock_is_smartswitch.assert_called_once_with()
            mock_is_dpu.assert_not_called()
            mock_platform_data.assert_called_once_with()
        elif is_dpu:
            mock_is_smartswitch.assert_called_once_with()
            mock_is_dpu.assert_called_once_with()
            mock_platform_data.assert_called_once_with()
        else:
            mock_is_smartswitch.assert_called_once_with()
            mock_is_dpu.assert_called_once_with()
            mock_platform_data.assert_not_called()

    def test_get_halt_services_timeout_matches_legacy_dpu_value(self):
        platform_data = {"dpu_halt_services_timeout": 120}
        with (
            mock.patch("host_modules.reboot.device_info.is_switch_host", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_smartswitch", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_dpu", return_value=True),
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=platform_data,
            ),
        ):
            assert host_reboot.get_halt_services_timeout() == 120

    def test_get_halt_services_timeout_keeps_legacy_invalid_dpu_log(self, caplog):
        platform_data = {"dpu_halt_services_timeout": "invalid"}
        with (
            mock.patch("host_modules.reboot.device_info.is_switch_host", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_smartswitch", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_dpu", return_value=True),
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=platform_data,
            ),
            caplog.at_level(logging.INFO),
        ):
            assert host_reboot.get_halt_services_timeout() == EXPECTED_HALT_TIMEOUT

        assert any(
            "Failed to read dpu_halt_services_timeout from platform.json" in record.message
            for record in caplog.records
        )

    def test_get_halt_services_timeout_unreadable_uses_default(self, caplog):
        with (
            mock.patch("host_modules.reboot.device_info.is_switch_host", return_value=True),
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=None,
            ),
            caplog.at_level(logging.INFO),
        ):
            assert host_reboot.get_halt_services_timeout() == EXPECTED_HALT_TIMEOUT

        assert any(
            "platform.json data is unavailable or not a dictionary" in record.message
            for record in caplog.records
        )

    def test_get_halt_services_timeout_identity_exception_uses_default(self, caplog):
        with mock.patch(
            "host_modules.reboot.device_info.is_switch_host",
            side_effect=RuntimeError("unexpected identity failure"),
        ), mock.patch(
            "host_modules.reboot.device_info.get_platform_json_data"
        ) as mock_platform_data, caplog.at_level(logging.INFO):
            assert host_reboot.get_halt_services_timeout() == EXPECTED_HALT_TIMEOUT

        mock_platform_data.assert_not_called()
        assert any(
            "Failed to resolve halt services timeout: unexpected identity failure" in
            record.message for record in caplog.records
        )

    def test_get_halt_services_timeout_unknown_identity_skips_platform_data(
            self, caplog):
        with (
            mock.patch("host_modules.reboot.device_info.is_switch_host", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_smartswitch", return_value=False),
            mock.patch("host_modules.reboot.device_info.is_dpu", return_value=False),
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
                return_value=None,
            ) as mock_platform_data,
            caplog.at_level(logging.INFO),
        ):
            assert (
                host_reboot.get_halt_services_timeout() ==
                EXPECTED_HALT_TIMEOUT
            )

        mock_platform_data.assert_not_called()
        assert not any(
            "platform.json data is unavailable or not a dictionary" in record.message
            for record in caplog.records
        )

    def test_switch_host_timeout_reader_does_not_resolve_identity(self):
        with (
            mock.patch.object(host_reboot.device_info, "is_switch_host") as identity,
            mock.patch.object(host_reboot.device_info, "get_platform_json_data",
                              return_value={"switch_host_halt_services_timeout": 90}),
        ):
            assert host_reboot.get_switch_host_halt_services_timeout() == 90
        identity.assert_not_called()

    def test_switch_host_timeout_reader_logs_loader_error(self, caplog):
        with (
            mock.patch.object(host_reboot.device_info, "get_platform_json_data",
                              side_effect=RuntimeError("reader failed")),
            caplog.at_level(logging.INFO),
        ):
            assert host_reboot.get_switch_host_halt_services_timeout() == EXPECTED_HALT_TIMEOUT
        assert "reader failed" in caplog.text

    @pytest.mark.parametrize("is_switch_host", [True, False])
    def test_strict_halt_checks_follow_switch_host_identity(self, is_switch_host):
        with (
            mock.patch(
                "host_modules.reboot.device_info.is_switch_host",
                return_value=is_switch_host,
            ) as mock_is_switch_host,
            mock.patch(
                "host_modules.reboot.device_info.get_platform_json_data",
            ) as mock_platform_data,
        ):
            assert host_reboot.is_strict_halt_check_enabled() is is_switch_host

        mock_is_switch_host.assert_called_once_with()
        mock_platform_data.assert_not_called()

    def test_strict_halt_checks_fall_back_on_unreadable_identity(self, caplog):
        with mock.patch(
            "host_modules.reboot.device_info.is_switch_host",
            side_effect=RuntimeError("unexpected identity failure"),
        ), caplog.at_level(logging.INFO):
            assert host_reboot.is_strict_halt_check_enabled() is False

        assert any("unexpected identity failure" in record.message for record in caplog.records)

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
        """WARM reboot reaches the post-sleep failure-flag path when reboot command returns success."""
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
        ):
            mock_run_command.return_value = (0, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.reboot_module.execute_reboot("WARM")
            mock_run_command.assert_called_once_with("sudo warm-reboot")
            mock_sleep.assert_called_once_with(260)
            mock_populate_reboot_status_flag.assert_called_once_with(False, TIME, "Reboot command failed to execute", 'WARM', RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_fail_unknown_reboot(self, caplog):
        with caplog.at_level(logging.ERROR):
            self.reboot_module.execute_reboot(-1)
            msg = "reboot: Unsupported reboot method: -1"
            assert caplog.records[0].message == msg

    def test_execute_reboot_fail_issue_reboot_command_cold_boot(self, caplog):
        """Cold-boot reboot command returning non-zero logs the error and records a failure timestamp."""
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute cold reboot"], ["stderror: execute cold reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_COLD_BOOT_ENUM)
            msg = ("reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute cold reboot'], stderr: "
                    "['stderror: execute cold reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, TIME, "Failed to execute reboot command", 1, RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_fail_issue_reboot_command_halt(self, caplog):
        """Halt reboot command returning non-zero logs the error and records a failure timestamp."""
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute halt reboot"], ["stderror: execute halt reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM)
            msg = ("reboot: Reboot failed execution with "
                   "stdout: ['stdout: execute halt reboot'], stderr: "
                   "['stderror: execute halt reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, TIME, "Failed to execute reboot command", 3, RebootStatus.STATUS_FAILURE)

    def test_get_dpu_halt_services_timeout_key_absent(self):
        mock_data = {}

        with mock.patch(
            "reboot.device_info.get_platform_json_data",
            return_value=mock_data,
        ):
            assert get_dpu_halt_services_timeout() == EXPECTED_HALT_TIMEOUT

    def test_execute_reboot_success_halt(self):
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False) as mock_is_halt_command_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=False) as mock_is_container_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
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
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.time.monotonic", side_effect=[0, 0, 5]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=True) as mock_is_halt_command_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=True) as mock_is_container_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            mock.patch("reboot.get_halt_services_timeout", return_value=5),
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (0, ["stdout: execute halt reboot"], ["stderror: execute halt reboot"])
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM)
            mock_run_command.assert_called_once_with("sudo reboot -p")
            mock_sleep.assert_called_with(5)
            mock_is_halt_command_running.assert_called()
            assert any("HALT reboot failed: Services are still running" in record.message for record in caplog.records)
            mock_populate_reboot_status_flag.assert_called_once_with(False, TIME, 'Halt reboot did not complete', 3, RebootStatus.STATUS_FAILURE)

    def test_execute_reboot_strict_transient_check_error_then_success(self):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("time.monotonic", side_effect=[0, 0, 1]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False) as mock_halt_running,
            mock.patch(
                "reboot.Reboot.is_container_running",
                side_effect=[Exception("transient"), False]
            ) as mock_pmon_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=5),
        ):
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True)

            mock_sleep.assert_called_once_with(5)
            assert mock_halt_running.call_count == 2
            assert mock_pmon_running.call_count == 2
            mock_populate.assert_called_once_with(
                False, 0, "Halt reboot completed", REBOOT_METHOD_HALT_BOOT_ENUM,
                RebootStatus.STATUS_SUCCESS
            )

    def test_execute_reboot_strict_happy_path(self):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False) as mock_halt_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=False) as mock_pmon_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
        ):
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True)

            mock_halt_running.assert_called_once_with(strict_checks=True)
            mock_pmon_running.assert_called_once_with("pmon", strict_checks=True)
            mock_populate.assert_called_once_with(
                False, 0, "Halt reboot completed", REBOOT_METHOD_HALT_BOOT_ENUM,
                RebootStatus.STATUS_SUCCESS
            )

    def test_execute_reboot_strict_docker_error_is_unanswerable(self, caplog):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("reboot.psutil.process_iter", return_value=[]),
            mock.patch("reboot.docker.from_env", side_effect=Exception("Docker error")) as mock_docker,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("time.monotonic", side_effect=[0, 0, 5]),
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=5),
            caplog.at_level(logging.ERROR),
        ):
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True)

            assert mock_docker.call_count == 2
            mock_sleep.assert_called_once_with(5)
            mock_populate.assert_called_once_with(
                False, TIME, "Halt completion check could not be answered",
                REBOOT_METHOD_HALT_BOOT_ENUM, RebootStatus.STATUS_FAILURE
            )

    def test_execute_reboot_strict_psutil_error_is_unanswerable(self, caplog):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("reboot.psutil.process_iter", side_effect=Exception("psutil error")) as mock_processes,
            mock.patch("reboot.docker.from_env") as mock_docker,
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("time.monotonic", side_effect=[0, 0, 5]),
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=5),
            caplog.at_level(logging.ERROR),
        ):
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True)

            assert mock_processes.call_count == 2
            mock_docker.assert_not_called()
            mock_sleep.assert_called_once_with(5)
            mock_populate.assert_called_once_with(
                False, TIME, "Halt completion check could not be answered",
                REBOOT_METHOD_HALT_BOOT_ENUM, RebootStatus.STATUS_FAILURE
            )

    def test_execute_reboot_strict_answered_timeout_keeps_existing_failure(self):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("time.monotonic", side_effect=[0, 0, 5]),
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=True) as mock_halt_running,
            mock.patch("reboot.Reboot.is_container_running", return_value=True) as mock_pmon_running,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=5),
        ):
            self.reboot_module.execute_reboot(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True)

            assert mock_halt_running.call_count == 2
            assert mock_pmon_running.call_count == 2
            mock_sleep.assert_called_once_with(5)
            mock_populate.assert_called_once_with(
                False, TIME, "Halt reboot did not complete", REBOOT_METHOD_HALT_BOOT_ENUM,
                RebootStatus.STATUS_FAILURE
            )

    def test_execute_reboot_dpu_errors_keep_legacy_success(self):
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("reboot.device_info.is_switch_host", return_value=False),
            mock.patch(
                "reboot.device_info.get_platform_json_data"
            ) as mock_platform_data,
            mock.patch("reboot.psutil.process_iter", side_effect=Exception("psutil error")),
            mock.patch("reboot.docker.from_env", side_effect=Exception("Docker error")),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
        ):
            strict_checks = is_strict_halt_check_enabled()
            self.reboot_module.execute_reboot(
                REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=strict_checks
            )

            assert strict_checks is False
            mock_platform_data.assert_not_called()
            mock_populate.assert_called_once_with(
                False, 0, "Halt reboot completed", REBOOT_METHOD_HALT_BOOT_ENUM,
                RebootStatus.STATUS_SUCCESS
            )

    def test_write_graceful_shutdown_reboot_cause_is_durable(self, tmp_path):
        reboot_cause_file = tmp_path / "reboot-cause.txt"
        reboot_cause_file.write_text("old cause")
        events = []
        real_fsync = os.fsync
        real_replace = os.replace

        def record_fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def record_replace(source, destination):
            events.append("replace")
            return real_replace(source, destination)

        with (
            mock.patch("reboot.REBOOT_CAUSE_DIR", str(tmp_path)),
            mock.patch("reboot.REBOOT_CAUSE_FILE", str(reboot_cause_file)),
            mock.patch("reboot.os.fsync", side_effect=record_fsync),
            mock.patch(
                "reboot.os.replace", side_effect=record_replace
            ) as mock_replace,
            mock.patch("reboot.os.open", wraps=os.open) as mock_open_directory,
        ):
            write_graceful_shutdown_reboot_cause()

        assert (
            reboot_cause_file.read_text() ==
            REBOOT_CAUSE_GRACEFUL_SHUTDOWN_FROM_BMC
        )
        assert not (tmp_path / "reboot-cause.txt.tmp").exists()
        assert events == ["fsync", "replace", "fsync"]
        mock_replace.assert_called_once_with(
            str(reboot_cause_file) + ".tmp", str(reboot_cause_file)
        )
        mock_open_directory.assert_called_once_with(str(tmp_path), os.O_RDONLY)

    def test_graceful_shutdown_reboot_cause_contract_literals(self):
        assert REBOOT_CAUSE_FILE == "/host/reboot-cause/reboot-cause.txt"
        assert (
            REBOOT_CAUSE_GRACEFUL_SHUTDOWN_FROM_BMC ==
            "graceful shutdown from BMC"
        )

    def test_execute_reboot_tagged_halt_writes_cause_before_success(self):
        events = []
        self.reboot_module.active_request_message = (
            "BMC pre-shutdown request "
            "[bmc-req:12345678-1234-4234-8234-123456789abc]"
        )

        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False),
            mock.patch("reboot.Reboot.is_container_running", return_value=False),
            mock.patch(
                "reboot.write_graceful_shutdown_reboot_cause",
                side_effect=lambda: events.append("write")
            ) as mock_write,
            mock.patch(
                "reboot.Reboot.populate_reboot_status_flag",
                side_effect=lambda *args: events.append(("status", args[-1]))
            ) as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
        ):
            self.reboot_module.execute_reboot(
                REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True
            )

        mock_write.assert_called_once_with()
        mock_populate.assert_called_once_with(
            False, 0, "Halt reboot completed", REBOOT_METHOD_HALT_BOOT_ENUM,
            RebootStatus.STATUS_SUCCESS
        )
        assert events == ["write", ("status", RebootStatus.STATUS_SUCCESS)]

    def test_execute_reboot_cause_write_failure_forfeits_success(self):
        self.reboot_module.active_request_message = (
            "BMC pre-shutdown request "
            "[bmc-req:12345678-1234-4234-8234-123456789abc]"
        )

        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False),
            mock.patch("reboot.Reboot.is_container_running", return_value=False),
            mock.patch(
                "reboot.write_graceful_shutdown_reboot_cause",
                side_effect=OSError("disk error")
            ),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
        ):
            self.reboot_module.execute_reboot(
                REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=True
            )

        mock_populate.assert_called_once_with(
            False, TIME, "Failed to write reboot cause", REBOOT_METHOD_HALT_BOOT_ENUM,
            RebootStatus.STATUS_FAILURE
        )

    @pytest.mark.parametrize(
        "request_message,is_switch_host,expected_strict_checks",
        [
            ("untagged request", True, True),
            (
                "[bmc-req:12345678-1234-4234-8234-123456789abc] "
                "[bmc-req:abcdefab-cdef-4abc-8def-abcdefabcdef]",
                True,
                True,
            ),
            (
                "BMC pre-shutdown request [bmc-req:12345678-1234-4234-8234-123456789abc]",
                False,
                False,
            ),
            (None, True, True),
        ],
        ids=[
            "untagged", "multiple-tags", "non-switch-host", "non-string-message",
        ],
    )
    def test_execute_reboot_unqualified_halt_preserves_existing_cause(
            self, request_message, is_switch_host, expected_strict_checks):
        self.reboot_module.active_request_message = request_message

        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch(
                "reboot.device_info.is_switch_host", return_value=is_switch_host
            ),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False),
            mock.patch("reboot.Reboot.is_container_running", return_value=False),
            mock.patch("reboot.write_graceful_shutdown_reboot_cause") as mock_write,
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate,
            mock.patch("reboot.get_halt_services_timeout", return_value=60),
        ):
            strict_checks = is_strict_halt_check_enabled()
            self.reboot_module.execute_reboot(
                REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=strict_checks
            )

        assert strict_checks is expected_strict_checks
        mock_write.assert_not_called()
        mock_populate.assert_called_once_with(
            False, 0, "Halt reboot completed", REBOOT_METHOD_HALT_BOOT_ENUM,
            RebootStatus.STATUS_SUCCESS
        )

    def test_execute_reboot_dpu_halt_keeps_status_contract(self):
        request_message = "DPU reboot request"
        dpu_halt_timeout = 120
        resolve_halt_timeout = get_halt_services_timeout
        resolved_timeouts = []

        def capture_halt_timeout():
            timeout = resolve_halt_timeout()
            resolved_timeouts.append(timeout)
            return timeout

        self.reboot_module.populate_reboot_status_flag(
            True, TIME, request_message, REBOOT_METHOD_HALT_BOOT_ENUM,
            RebootStatus.STATUS_UNKNOWN
        )
        with (
            mock.patch("reboot._run_command", return_value=(0, [], [])),
            mock.patch("reboot.device_info.is_switch_host", return_value=False),
            mock.patch("reboot.device_info.is_smartswitch", return_value=False),
            mock.patch("reboot.device_info.is_dpu", return_value=True) as mock_is_dpu,
            mock.patch(
                "reboot.device_info.get_platform_json_data",
                return_value={"dpu_halt_services_timeout": dpu_halt_timeout},
            ) as mock_platform_data,
            mock.patch(
                "reboot.get_halt_services_timeout",
                side_effect=capture_halt_timeout,
            ),
            mock.patch("time.monotonic", side_effect=[0, 0]),
            mock.patch("reboot.Reboot.is_halt_command_running", return_value=False),
            mock.patch("reboot.Reboot.is_container_running", return_value=False),
            mock.patch("reboot.write_graceful_shutdown_reboot_cause") as mock_write,
        ):
            strict_checks = is_strict_halt_check_enabled()
            self.reboot_module.execute_reboot(
                REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks=strict_checks
            )

        _, response = self.reboot_module.get_reboot_status()
        response_data = json.loads(response)
        assert strict_checks is False
        mock_is_dpu.assert_called_once_with()
        mock_platform_data.assert_called_once_with()
        assert resolved_timeouts == [dpu_halt_timeout]
        mock_write.assert_not_called()
        assert response_data["active"] is False
        assert response_data["reason"] == "Halt reboot completed | {}".format(request_message)
        assert response_data["status"] == {
            "status": RebootStatus.STATUS_SUCCESS.value,
            "message": "",
        }

    def test_execute_reboot_fail_issue_reboot_command_warm(self, caplog):
        """WARM reboot command returning non-zero logs the error and records a failure timestamp."""
        with (
            mock.patch("reboot._run_command") as mock_run_command,
            mock.patch("time.time", return_value=TIME),
            mock.patch("reboot.Reboot.populate_reboot_status_flag") as mock_populate_reboot_status_flag,
            caplog.at_level(logging.ERROR),
        ):
            mock_run_command.return_value = (1, ["stdout: execute WARM reboot"], ["stderror: execute WARM reboot"])
            self.reboot_module.execute_reboot("WARM")
            msg = ("reboot: Reboot failed execution with "
                    "stdout: ['stdout: execute WARM reboot'], stderr: "
                    "['stderror: execute WARM reboot']")
            assert caplog.records[0].message == msg
            mock_populate_reboot_status_flag.assert_called_once_with(False, TIME, "Failed to execute reboot command", 'WARM', RebootStatus.STATUS_FAILURE)

    def test_issue_reboot_success_cold_boot(self):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
        ):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_COLD])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            assert self.reboot_module.active_request_message == "test reboot request reason"
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

    @pytest.mark.parametrize("strict_checks", [True, False])
    def test_issue_reboot_success_halt(self, strict_checks):
        with (
            mock.patch("threading.Thread") as mock_thread,
            mock.patch("reboot.Reboot.validate_reboot_request", return_value=(0, "")),
            mock.patch("reboot.is_strict_halt_check_enabled", return_value=strict_checks) as mock_strict_gate,
        ):
            self.reboot_module.populate_reboot_status_flag()
            result = self.reboot_module.issue_reboot([VALID_REBOOT_REQUEST_HALT])
            assert result[0] == 0
            assert result[1] == "Successfully issued reboot"
            mock_strict_gate.assert_called_once_with()
            mock_thread.assert_called_once_with(
                target=self.reboot_module.execute_reboot,
                args=(REBOOT_METHOD_HALT_BOOT_ENUM, strict_checks),
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
