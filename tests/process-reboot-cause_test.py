import os
import sys
import unittest
from unittest.mock import patch, mock_open, MagicMock
from io import StringIO
import importlib.util

# Paths setup
test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
process_reboot_cause_path = os.path.join(scripts_path, 'process-reboot-cause')

# Check if the file exists (ensure mocks don’t interfere)
real_exists = os.path.exists
if not real_exists(process_reboot_cause_path):
    raise FileNotFoundError(f"File not found: {process_reboot_cause_path}")

def load_module_from_source(module_name, path):
    if not os.access(path, os.R_OK):
        raise PermissionError(f"File is not readable: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise ImportError(f"Could not load spec for {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise ImportError(f"No loader found for module: {module_name}")
    spec.loader.exec_module(module)
    return module

# Load the module
process_reboot_cause = load_module_from_source('process_reboot_cause', process_reboot_cause_path)

class TestProcessRebootCause(unittest.TestCase):
    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "PowerLoss", "user": "admin", "time": "2024-12-10", "comment": "test"}')
    @patch("os.listdir", return_value=["file1.json", "file2.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", side_effect=lambda path: real_exists(path) or path.endswith('file1.json') or path.endswith('file2.json'))
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    def test_process_reboot_cause(self, mock_stdout, mock_is_smartswitch, mock_connector, mock_remove, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.main()

        # Validate syslog and stdout logging
        output = mock_stdout.getvalue()
        self.assertIn("Starting up...", output)
        self.assertIn("Previous reboot cause: User issued", output)

        # Verify DB interactions
        mock_db.connect.assert_called()
        self.assertTrue(mock_db.set.called)

    @patch("builtins.open", new_callable=mock_open, read_data='{"invalid_json}')
    @patch("os.listdir", return_value=["file1.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", side_effect=lambda path: real_exists(path) or path.endswith('file1.json'))
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    def test_invalid_json(self, mock_stdout, mock_is_smartswitch, mock_connector, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock DB
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            process_reboot_cause.main()

        # Check invalid JSON handling
        output = mock_stdout.getvalue()
        self.assertIn("Starting up...", output)
        self.assertIn("Invalid JSON format", output)  # Adjust based on your script
        self.assertTrue(mock_connector.called)