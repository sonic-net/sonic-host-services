"""Tests for gnoi_reset."""

import imp
import os
import sys
import logging

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

# Input requests based on gNOI CLI examples (snake_case and camelCase mix)
VALID_RESET_REQUEST = '{"factory_os": true}'
ZERO_FILL_REQUEST = '{"factory_os": true, "zero_fill": true}'
RETAIN_CERTS_REQUEST = '{"factory_os": true, "retainCerts": true}'
INVALID_RESET_REQUEST = '"factory_os": true, "zero_fill": true'

imp.load_source("host_service", host_modules_path + "/host_service.py")
imp.load_source("gnoi_reset", host_modules_path + "/gnoi_reset.py")
from gnoi_reset import *


class TestGnoiReset:
    @classmethod
    def setup_class(cls):
        with mock.patch("gnoi_reset.super"):
            cls.gnoi_reset_module = GnoiReset(MOD_NAME)

    def test_zero_fill_unsupported(self):
        result = self.gnoi_reset_module.issue_reset(ZERO_FILL_REQUEST)
        assert result[0] == 0
        assert result[1] == (
            '{"reset_error": {"zero_fill_unsupported": true, '
            '"detail": "zero_fill operation is currently unsupported."}}'
        )

    def test_retain_certs_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = self.gnoi_reset_module.issue_reset(RETAIN_CERTS_REQUEST)
            assert (
                caplog.records[0].message
                == "gnoi_reset: retain_certs is currently ignored."
            )
            assert result[0] == 0
            assert result[1] == (
                '{"reset_error": {"other": true, '
                '"detail": "Method FactoryReset.Start is currently unsupported."}}'
            )

    def test_invalid_json_format(self):
        result = self.gnoi_reset_module.issue_reset(INVALID_RESET_REQUEST)
        assert result[0] == 0
        assert result[1] == (
            '{"reset_error": {"other": true, '
            '"detail": "Failed to parse json formatted factory reset request into python dict."}}'
        )

    def test_valid_request_unimplemented(self):
        result = self.gnoi_reset_module.issue_reset(VALID_RESET_REQUEST)
        assert result[0] == 0
        assert result[1] == (
            '{"reset_error": {"other": true, '
            '"detail": "Method FactoryReset.Start is currently unsupported."}}'
        )

    def test_populate_reset_response_success(self):
        _, response = self.gnoi_reset_module.populate_reset_response(reset_success=True)
        assert response == '{"reset_success": {}}'

    def test_populate_reset_response_other_error(self):
        _, response = self.gnoi_reset_module.populate_reset_response(
            reset_success=False,
            detail="Generic failure."
        )
        assert response == (
            '{"reset_error": {"other": true, "detail": "Generic failure."}}'
        )

    def test_check_reboot_in_progress_true(self):
        self.gnoi_reset_module.is_reset_ongoing = True
        rc, result = self.gnoi_reset_module._check_reboot_in_progress()
        assert rc == 0
        assert result == ""

    def test_check_reboot_in_progress_false(self):
        self.gnoi_reset_module.is_reset_ongoing = False
        rc, result = self.gnoi_reset_module._check_reboot_in_progress()
        assert rc == 0
        assert result == (
            '{"reset_error": {"other": true, "detail": "Previous reset is ongoing."}}'
        )

    @mock.patch("gnoi_reset.Reboot")
    @mock.patch("threading.Thread")
    def test_execute_reboot_success(self, mock_thread_cls, mock_reboot_cls):
        mock_reboot_instance = mock.Mock()
        mock_reboot_cls.return_value = mock_reboot_instance

        mock_thread_instance = mock.Mock()
        mock_thread_cls.return_value = mock_thread_instance

        rc, msg = self.gnoi_reset_module._execute_reboot()

        mock_reboot_cls.assert_called_once_with("reboot")
        mock_thread_cls.assert_called_once_with(
            target=mock_reboot_instance.execute_reboot,
            args=("COLD",)
        )
        mock_thread_instance.start.assert_called_once()
        assert rc == 0
        assert msg == ""

    @mock.patch("gnoi_reset.Reboot")
    @mock.patch("threading.Thread", side_effect=RuntimeError)
    def test_execute_reboot_runtime_error(self, mock_thread_cls, mock_reboot_cls):
        self.gnoi_reset_module.is_reset_ongoing = True

        rc, msg = self.gnoi_reset_module._execute_reboot()

        assert rc == 0
        assert msg == (
            '{"reset_error": {"other": true, '
            '"detail": "Failed to start thread to execute reboot."}}'
        )
        assert self.gnoi_reset_module.is_reset_ongoing is False

    def test_register(self):
        result = register()
        assert result[0] == GnoiReset
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
