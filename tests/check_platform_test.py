import sys
import os
from unittest.mock import patch, MagicMock
import unittest
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

import check_platform

class TestCheckPlatform(unittest.TestCase):

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('check_platform.subprocess.run')
    def test_smart_switch_npu(self, mock_subprocess_run, mock_is_dpu):
        """Test case for SmartSwitch NPU platform."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="SmartSwitch", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 0)

    @patch('utilities_common.chassis.is_dpu', return_value=True)
    @patch('check_platform.subprocess.run')
    def test_dpu_platform(self, mock_subprocess_run, mock_is_dpu):
        """Test case for DPU platform."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="SmartSwitch", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('check_platform.subprocess.run')
    def test_other_platform(self, mock_subprocess_run, mock_is_dpu):
        """Test case for other platforms."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="Other", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('check_platform.subprocess.run', side_effect=Exception("Test error"))
    def test_exception(self, mock_subprocess_run):
        """Test case for exception during subprocess execution."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('check_platform.subprocess.run')
    def test_is_dpu_import_error(self, mock_subprocess_run):
        """Test case when is_dpu import fails."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="SmartSwitch", stderr="")
        # Mock the import to raise an exception
        with patch('builtins.__import__', side_effect=ImportError("Module not found")):
            with self.assertRaises(SystemExit) as cm:
                check_platform.main()
            # Should exit with 0 because is_dpu_platform will be False (from exception)
            # and subtype is "SmartSwitch"
            self.assertEqual(cm.exception.code, 0)

    @patch('utilities_common.chassis.is_dpu', side_effect=RuntimeError("DPU check failed"))
    @patch('check_platform.subprocess.run')
    def test_is_dpu_exception(self, mock_subprocess_run, mock_is_dpu):
        """Test case when is_dpu() raises an exception."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="SmartSwitch", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        # is_dpu_platform will be False due to exception, so SmartSwitch + not DPU = exit 0
        self.assertEqual(cm.exception.code, 0)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('check_platform.subprocess.run')
    def test_empty_subtype(self, mock_subprocess_run, mock_is_dpu):
        """Test case when subtype is empty."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('check_platform.subprocess.run')
    def test_subtype_with_whitespace(self, mock_subprocess_run, mock_is_dpu):
        """Test case when subtype has leading/trailing whitespace."""
        mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="  SmartSwitch  \n", stderr="")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 0)

    @patch('check_platform.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd=['sonic-cfggen'], timeout=5))
    def test_subprocess_timeout(self, mock_subprocess_run):
        """Test case when subprocess times out."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('check_platform.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'sonic-cfggen'))
    def test_subprocess_error(self, mock_subprocess_run):
        """Test case when subprocess returns an error."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('check_platform.subprocess.run')
    def test_case_sensitive_subtype(self, mock_subprocess_run, mock_is_dpu):
        """Test case for case sensitivity of subtype check."""
        mock_subprocess_run.return_value = MagicMock(stdout="smartswitch")
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        # Should exit 1 because "smartswitch" != "SmartSwitch" (case sensitive)
        self.assertEqual(cm.exception.code, 1)

if __name__ == '__main__':
    unittest.main()
