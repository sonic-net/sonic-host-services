"""Tests for gnoi_reset."""

import imp
import os
import sys

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

VALID_RESET_REQUEST = '{"factoryOs": true}'
LEGACY_RESET_REQUEST = "{}"
ZERO_FILL_REQUEST = '{"factoryOs": true, "zeroFill": true}'
RETAIN_CERTS_REQUEST = '{"factoryOs": true, "retainCerts": true}'
INVALID_RESET_REQUEST = '"factoryOs": true, "zeroFill": true'

imp.load_source("host_service", host_modules_path + "/host_service.py")
imp.load_source("infra_host", host_modules_path + "/infra_host.py")
imp.load_source("gnoi_reset", host_modules_path + "/gnoi_reset.py")
from gnoi_reset import *


class TestGnoiReset(object):
    @classmethod
    def setup_class(cls):
        with mock.patch("gnoi_reset.super") as mock_host_module:
            cls.gnoi_reset_module = GnoiReset(MOD_NAME)

    def test_populate_response_factory_os_unsupported(self):
        self.gnoi_reset_module.populate_reset_response(
            reset_success=False, factory_os_unsupported=True, detail="test"
        )
        assert self.gnoi_reset_module.reset_response == {
            "reset_error": {"factory_os_unsupported": True, "detail": "test"}
        }

    def test_execute_reboot_success(self):
        with mock.patch.object(
            infra_host.InfraHost,
            "_run_command",
            return_value=(0, ["stdout: reboot"], ["stderror: reboot"]),
        ):
            with mock.patch(
                "host_modules.infra_host.InfraHost.raise_critical_state"
            ) as mock_raise_critical_state:
                with mock.patch("time.sleep") as mock_sleep:
                    self.gnoi_reset_module.execute_reboot()
                    mock_sleep.assert_called_once_with(260)
                    mock_raise_critical_state.assert_called_once()

    def test_execute_reboot_fail_run_command(self, caplog):
        with mock.patch.object(
            infra_host.InfraHost,
            "_run_command",
            return_value=(1, ["stdout: reboot"], ["stderror: reboot"]),
        ):
            with caplog.at_level(logging.ERROR):
                self.gnoi_reset_module.execute_reboot()
                msg = (
                    "gnoi_reset: Cold reboot failed execution with stdout: "
                    "['stdout: reboot'], stderr: ['stderror: reboot']."
                )
                assert caplog.records[0].message == msg

    def test_zero_fill_unsupported(self):
        self.gnoi_reset_module.is_reset_ongoing = False
        result = self.gnoi_reset_module.issue_reset(ZERO_FILL_REQUEST)
        assert result[0] == 0
        assert result[1] == (
            '{"reset_error": {"zero_fill_unsupported": true, "detail": "zero_fill operation is currently unsupported."}}'
        )

    def test_retain_certs_warning(self, caplog):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            with mock.patch("threading.Thread") as mock_thread:
                with caplog.at_level(logging.WARNING):
                    mock_run_command.side_effect = [
                        (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], []),
                        (0, ["stdout: success"], []),
                        (0, ["stdout: success"], []),
                    ]
                    self.gnoi_reset_module.is_reset_ongoing = False
                    result = self.gnoi_reset_module.issue_reset(RETAIN_CERTS_REQUEST)
                    assert (
                        caplog.records[0].message
                        == "gnoi_reset: retain_certs is currently ignored."
                    )
                    assert result[0] == 0
                    assert result[1] == '{"reset_success": {}}'
                    mock_thread.assert_called_once_with(
                        target=self.gnoi_reset_module.execute_reboot
                    )
                    mock_thread.return_value.start.assert_called_once_with()

    def test_issue_reset_bad_format_reset_request(self):
        self.gnoi_reset_module.is_reset_ongoing = False
        result = self.gnoi_reset_module.issue_reset(INVALID_RESET_REQUEST)
        assert result[0] == 0
        assert result[1] == (
            '{"reset_error": {"other": true, "detail": '
            '"Failed to parse json formatted factory reset '
            'request into python dict."}}'
        )

    def test_issue_reset_success(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            with mock.patch("gnoi_reset.EXECUTE_CLEANUP_COMMAND", ["rm -rf /mnt"]):
                with mock.patch("threading.Thread") as mock_thread:
                    mock_run_command.side_effect = [
                        (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], []),
                        (0, ["stdout: success"], []),
                        (0, ["stdout: success"], []),
                    ]
                    self.gnoi_reset_module.is_reset_ongoing = False
                    result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
                    assert result[0] == 0
                    assert result[1] == '{"reset_success": {}}'
                    mock_thread.assert_called_once_with(
                        target=self.gnoi_reset_module.execute_reboot
                    )
                    mock_thread.return_value.start.assert_called_once_with()

    def test_get_boot_install_value_fail(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            mock_run_command.return_value = (
                1,
                ["stdout:"],
                ["stderror: failed to get boot install value"],
            )
            self.gnoi_reset_module.is_reset_ongoing = False
            result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, '
                '"detail": "Failed to get the boot '
                'install value."}}'
            )

    def test_issue_reset_previous_reset_ongoing(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            mock_run_command.side_effect = [
                (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], [])
            ]
            self.gnoi_reset_module.is_reset_ongoing = True
            result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
            self.gnoi_reset_module.is_reset_ongoing = False
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, "detail": '
                '"Previous reset is ongoing."}}'
            )

    def test_get_boot_install_value_index_error_fail(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            mock_run_command.return_value = (0, ["regionselect=a", "bootinstall=1"], [])
            self.gnoi_reset_module.is_reset_ongoing = False
            result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, '
                '"detail": "Failed to get the boot install '
                'value with error: list index out of range."'
                "}}"
            )

    def test_get_boot_install_value_value_error_fail(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            mock_run_command.return_value = (
                0,
                ["regionselect=a", "bootcount=0", "bootinstall=a"],
                [],
            )
            self.gnoi_reset_module.is_reset_ongoing = False
            result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, '
                '"detail": "Failed to get the boot install '
                "value with error: invalid literal for int() "
                "with base 10: 'a'.\"}}"
            )

    def test_net_boot_not_complete_duplicate_request(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            mock_run_command.return_value = (
                0,
                ["regionselect=a", "bootcount=0", "bootinstall=1"],
                [],
            )
            self.gnoi_reset_module.is_reset_ongoing = False
            result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, '
                '"detail": "Previous reset is ongoing."}}'
            )

    def test_issue_reset_fail_issue_boot_install_command(self, caplog):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            with caplog.at_level(logging.ERROR):
                mock_run_command.side_effect = [
                    (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], []),
                    (
                        1,
                        ["stdout: failed to execute boot install command"],
                        ["stderror: failed to execute boot install command"],
                    ),
                ]
                self.gnoi_reset_module.is_reset_ongoing = False
                result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
                assert result[0] == 0
                assert result[1] == (
                    '{"reset_error": {"other": true, '
                    '"detail": "Boot count execution '
                    'failed."}}'
                )
                msg = (
                    "gnoi_reset: Boot count execution with stdout: "
                    "['stdout: failed to execute boot install command'], "
                    "stderr: ['stderror: failed to execute boot install "
                    "command']."
                )
                assert caplog.records[0].message == msg

    def raise_runtime_exception_test(self):
        raise RuntimeError("test raise RuntimeError exception")

    def test_issue_reset_fail_issue_reboot_thread(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            with mock.patch("threading.Thread") as mock_thread:
                mock_run_command.side_effect = [
                    (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], []),
                    (0, ["stdout: success"], []),
                ]
                mock_thread.return_value.start = self.raise_runtime_exception_test
                self.gnoi_reset_module.is_reset_ongoing = False
                result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
                assert result[0] == 0
                assert result[1] == (
                    '{"reset_error": {"other": true, '
                    '"detail": "Failed to start thread to '
                    'execute reboot."}}'
                )

    def test_cleanup_failed(self):
        with mock.patch.object(
            infra_host.InfraHost, "_run_command"
        ) as mock_run_command:
            with mock.patch("gnoi_reset.EXECUTE_CLEANUP_COMMAND", ["rm -rf /mnt"]):
                with mock.patch("threading.Thread") as mock_thread:
                    mock_run_command.side_effect = [
                        (0, ["regionselect=a", "bootcount=0", "bootinstall=0"], []),
                        (
                            1,
                            ["stdout: failed to execute cleanup command"],
                            ["stderror: failed to execute cleanup command"],
                        ),
                        (0, ["stdout: success"], [])
                    ]
                    self.gnoi_reset_module.is_reset_ongoing = False
                    result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
                    assert result[0] == 0
                    assert result[1] == '{"reset_success": {}}'
                    mock_thread.assert_called_once_with(
                        target=self.gnoi_reset_module.execute_reboot
                    )
                    mock_thread.return_value.start.assert_called_once_with()


    def test_register(self):
        result = register()
        assert result[0] == GnoiReset
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
