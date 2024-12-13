import sys
import os
from unittest import TestCase
from unittest.mock import patch, MagicMock, mock_open
from io import StringIO
from sonic_py_common.general import load_module_from_source

# Mock the connector
from .mock_connector import MockConnector
import swsscommon

# Mock the SonicV2Connector
swsscommon.SonicV2Connector = MockConnector

# Define the path to the script and load it using the helper function
test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the process-reboot-cause module using the helper function
process_reboot_cause_path = os.path.join(scripts_path, "process-reboot-cause")
process_reboot_cause = load_module_from_source('process_reboot_cause', process_reboot_cause_path)

# Now proceed with your test class and mocks
class TestProcessRebootCause(TestCase):
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "PowerLoss", "user": "admin", "time": "2024-12-10", "comment": "test"}')
    @patch("os.listdir", return_value=["file1.json", "file2.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", side_effect=lambda path: path.endswith('file1.json') or path.endswith('file2.json'))
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    def test_process_reboot_cause(self, mock_geteuid, mock_stdout, mock_is_smartswitch, mock_connector, mock_remove, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.main()

        # Validate syslog and stdout logging
        output = mock_stdout.getvalue()

        # Verify DB interactions
        mock_db.connect.assert_called()

    @patch("builtins.open", new_callable=mock_open, read_data='{"invalid_json}')
    @patch("os.listdir", return_value=["file1.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", side_effect=lambda path: path.endswith('file1.json'))
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    def test_invalid_json(self, mock_geteuid, mock_stdout, mock_is_smartswitch, mock_connector, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.main()

        # Check invalid JSON handling
        output = mock_stdout.getvalue()
        self.assertTrue(mock_connector.called)

    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1", "dpu2"])
    def test_update_dpu_reboot_cause_to_chassis_state_db(self, mock_get_dpu_list, mock_connector):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Call the function
        process_reboot_cause.update_dpu_reboot_cause_to_chassis_state_db()

        # Verify DB interactions for each DPU
        mock_db.set.assert_any_call(mock_db.CHASSIS_STATE_DB, "REBOOT_CAUSE|DPU1", "cause", "PowerLoss")
        mock_db.set.assert_any_call(mock_db.CHASSIS_STATE_DB, "REBOOT_CAUSE|DPU1", "time", "2024-12-10")

    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=[])
    def test_invalid_dpu_list(self, mock_get_dpu_list):
        # Call the function with an empty DPU list
        process_reboot_cause.update_dpu_reboot_cause_to_chassis_state_db()

        # Ensure no DB interactions occur when DPU list is empty
        mock_get_dpu_list.assert_called_once()
