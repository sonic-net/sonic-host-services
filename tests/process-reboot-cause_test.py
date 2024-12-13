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

    # Test get_sorted_reboot_cause_files
    @patch("process_reboot_cause.os.listdir")
    @patch("process_reboot_cause.os.path.getmtime")
    def test_get_sorted_reboot_cause_files_success(self, mock_getmtime, mock_listdir):
        # Setup mock data
        mock_listdir.return_value = ["file1.txt", "file2.txt", "file3.txt"]
        mock_getmtime.side_effect = [100, 200, 50]  # Mock modification times

        # Call the function
        result = process_reboot_cause.get_sorted_reboot_cause_files("/mock/dpu_history")

        # Assert the files are sorted by modification time in descending order
        self.assertEqual(result, [
            "/mock/dpu_history/file2.txt",
            "/mock/dpu_history/file1.txt",
            "/mock/dpu_history/file3.txt"
        ])

    @patch("process_reboot_cause.os.listdir")
    def test_get_sorted_reboot_cause_files_error(self, mock_listdir):
        # Simulate an exception during file listing
        mock_listdir.side_effect = Exception("Mocked error")

        # Call the function and check the result
        result = process_reboot_cause.get_sorted_reboot_cause_files("/mock/dpu_history")
        self.assertEqual(result, [])

    # Test update_dpu_reboot_cause_to_chassis_state_db
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "Non-Hardware", "comment": "Switch rebooted DPU", "device": "DPU0", "time": "Fri Dec 13 01:12:36 AM UTC 2024", "name": "2024_12_13_01_12_36"}')
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1", "dpu2"])
    @patch("os.path.isfile", return_value=True)
    @patch("process_reboot_cause.get_sorted_reboot_cause_files")
    @patch("process_reboot_cause.os.listdir", return_value=["2024_12_13_01_12_36_reboot_cause.txt", "2024_12_14_01_11_46_reboot_cause.txt"])
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    def test_update_dpu_reboot_cause_to_chassis_state_db_update(self, mock_connector,  mock_listdir,  mock_get_sorted_files, mock_isfile, mock_get_dpu_list, mock_open):
        # Setup mocks
        mock_get_sorted_files.return_value = ["/mock/dpu_history/2024_12_13_01_12_36_reboot_cause.txt"]

        # Mock the database connection
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Call the function that reads the file and updates the DB
        process_reboot_cause.update_dpu_reboot_cause_to_chassis_state_db()
