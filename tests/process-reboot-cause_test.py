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
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "Non-Hardware", "user": "", "comment": "Switch rebooted DPU", "device": "DPU0", "time": "Fri Dec 13 01:12:36 AM UTC 2024", "gen_time": "2024_12_13_01_12_36"}')
    @patch("os.listdir", return_value=["file1.json", "file2.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", side_effect=lambda path: 1700000000 if "file1.json" in path else 1700001000)
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=False)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1"])
    def test_process_reboot_cause(self, mock_get_dpu_list, mock_geteuid, mock_stdout, mock_is_smartswitch, mock_connector, mock_remove, mock_getmtime, mock_exists, mock_isfile, mock_listdir, mock_open):
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

    @patch("builtins.open", new_callable=mock_open, read_data='{"invalid_json": ')  # Malformed JSON
    @patch("os.listdir", return_value=["file1.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", side_effect=lambda path: 1700000000 if "file1.json" in path else 1700001000)
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=False)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1", "dpu2"])
    def test_invalid_json(
        self, mock_get_dpu_list, mock_geteuid, mock_stdout, mock_is_smartswitch, 
        mock_connector, mock_remove, mock_getmtime, mock_exists, mock_isfile, 
        mock_listdir, mock_open
    ):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            try:
                process_reboot_cause.read_reboot_cause_files_and_save_to_db('npu')
            except json.JSONDecodeError:
                pass  # Expected failure due to invalid JSON

        # Check invalid JSON handling
        output = mock_stdout.getvalue()

    # Test read_reboot_cause_files_and_save_to_db - smartswitch
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "Non-Hardware", "user": "admin", "name": "2024_12_13_01_12_36", "comment": "Switch rebooted DPU", "device": "DPU0", "time": "Fri Dec 13 01:12:36 AM UTC 2024"}')
    @patch("os.listdir", return_value=["file1.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", side_effect=lambda path: 1700000000 if "file1.json" in path else 1700001000)
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1"])
    def test_read_reboot_cause_files_and_save_to_db(
        self, mock_get_dpu_list, mock_geteuid, mock_stdout, mock_is_smartswitch,
        mock_connector, mock_remove, mock_getmtime, mock_exists, mock_isfile,
        mock_listdir, mock_open
    ):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.read_reboot_cause_files_and_save_to_db('dpu1')

    # Test read_reboot_cause_files_and_save_to_db - smartswitch - name not in data
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "Non-Hardware", "user": "admin", "comment": "Switch rebooted DPU", "device": "DPU0", "time": "Fri Dec 13 01:12:36 AM UTC 2024"}')
    @patch("os.listdir", return_value=["file1.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", side_effect=lambda path: 1700000000 if "file1.json" in path else 1700001000)
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1"])
    def test_read_reboot_cause_files_name_not_in_data(
        self, mock_get_dpu_list, mock_geteuid, mock_stdout, mock_is_smartswitch,
        mock_connector, mock_remove, mock_getmtime, mock_exists, mock_isfile,
        mock_listdir, mock_open
    ):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.read_reboot_cause_files_and_save_to_db('dpu1')

    # test_process_reboot_cause_with_old_files
    @patch("process_reboot_cause.MAX_HISTORY_FILES", new=2)
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "Non-Hardware", "user": "admin", "name": "2024_12_13_01_12_36", "comment": "Switch rebooted DPU", "device": "DPU1", "time": "Fri Dec 13 01:12:36 AM UTC 2024"}')
    @patch("os.listdir", return_value=["file1.json", "file2.json", "file3.json", "file4.json", "prev_reboot_time.txt"])
    @patch("os.path.isfile", side_effect=lambda path: not path.endswith("prev_reboot_time.txt"))
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getmtime", side_effect=lambda path: {
        "file1.json": 1700000000,
        "file2.json": 1700001000,
        "file3.json": 1700002000,
        "file4.json": 1700003000
    }[os.path.basename(path)])
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    @patch("os.geteuid", return_value=0)
    @patch("process_reboot_cause.device_info.get_dpu_list", return_value=["dpu1"])
    @patch("process_reboot_cause.sorted")  # Patch sorting to capture list
    def test_process_reboot_cause_with_old_files(self, mock_sorted, mock_get_dpu_list, mock_geteuid,
                                                mock_stdout, mock_is_smartswitch, mock_connector, mock_remove,
                                                mock_getmtime, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Capture sorted file list
        def sorted_side_effect(iterable, *args, **kwargs):
            sorted_list = sorted(iterable, *args, **kwargs)  # Use real sorting
            print(f"DEBUG: TIME_SORTED_FULL_REBOOT_FILE_LIST = {sorted_list}")
            return sorted_list

        mock_sorted.side_effect = sorted_side_effect

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.read_reboot_cause_files_and_save_to_db('dpu1')
        
        # Ensure the correct number of old history files are removed
        mock_remove.assert_any_call("/host/reboot-cause/module/dpu1/history/file1.json")