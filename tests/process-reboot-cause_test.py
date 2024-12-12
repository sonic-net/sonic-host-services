import unittest
import os
import sys
from unittest.mock import patch, mock_open, MagicMock
from io import StringIO

def load_module_from_source(module_name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class TestProcessRebootCause(unittest.TestCase):
    def setUp(self):
        # Setup the path
        self.test_path = os.path.dirname(os.path.abspath(__file__))
        self.modules_path = os.path.dirname(test_path)
        self.scripts_path = os.path.join(modules_path, "scripts")
        self.sys.path.insert(0, modules_path)
        self.process_reboot_cause_path = os.path.join(self.scripts_path, 'process-reboot-cause.py')
        self.process_reboot_cause = load_module_from_source('process_reboot_cause', self.process_reboot_cause_path)

    @patch("builtins.open", new_callable=mock_open, read_data='{"cause": "PowerLoss", "user": "admin", "time": "2024-12-10", "comment": "test"}')
    @patch("os.listdir", return_value=["file1.json", "file2.json"])
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    @patch("sys.stdout", new_callable=StringIO)
    def test_process_reboot_cause(self, mock_stdout, mock_is_smartswitch, mock_connector, mock_remove, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock the SonicV2Connector behavior
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            self.process_reboot_cause.main()

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
    @patch("os.path.exists", return_value=True)
    @patch("process_reboot_cause.swsscommon.SonicV2Connector")
    @patch("process_reboot_cause.device_info.is_smartswitch", return_value=True)
    def test_invalid_json(self, mock_is_smartswitch, mock_connector, mock_exists, mock_isfile, mock_listdir, mock_open):
        # Mock the SonicV2Connector behavior
        mock_db = MagicMock()
        mock_connector.return_value = mock_db

        # Simulate running the script
        with patch.object(sys, "argv", ["process-reboot-cause"]):
            self.process_reboot_cause.main()

        # Check that invalid JSON does not break execution
        self.assertTrue(mock_connector.called)
