import sys
import os
from unittest.mock import patch
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

import check_platform

class TestCheckPlatform(unittest.TestCase):

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('sonic_py_common.device_info.is_smartswitch', return_value=True)
    def test_smart_switch_npu(self, mock_is_smartswitch, mock_is_dpu):
        """Test case for SmartSwitch NPU platform."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 0)

    @patch('utilities_common.chassis.is_dpu', return_value=True)
    @patch('sonic_py_common.device_info.is_smartswitch', return_value=True)
    def test_dpu_platform(self, mock_is_smartswitch, mock_is_dpu):
        """Test case for DPU platform (SmartSwitch but is DPU)."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('sonic_py_common.device_info.is_smartswitch', return_value=False)
    def test_other_platform(self, mock_is_smartswitch, mock_is_dpu):
        """Test case for other platforms (not SmartSwitch)."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('sonic_py_common.device_info.is_smartswitch', side_effect=ImportError("Test error"))
    def test_exception(self, mock_is_smartswitch):
        """Test case for exception during is_smartswitch check."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', side_effect=AttributeError("DPU check failed"))
    @patch('sonic_py_common.device_info.is_smartswitch', return_value=True)
    def test_is_dpu_exception(self, mock_is_smartswitch, mock_is_dpu):
        """Test case when is_dpu() raises an exception."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

    @patch('utilities_common.chassis.is_dpu', return_value=False)
    @patch('sonic_py_common.device_info.is_smartswitch', side_effect=ImportError("Module not found"))
    def test_is_smartswitch_import_error(self, mock_is_smartswitch, mock_is_dpu):
        """Test case when is_smartswitch import fails."""
        with self.assertRaises(SystemExit) as cm:
            check_platform.main()
        self.assertEqual(cm.exception.code, 1)

if __name__ == '__main__':
    unittest.main()
