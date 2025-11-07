import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import gnoi_shutdown_daemon

# Common fixtures
mock_message = {
    "type": "pmessage",
    "channel": f"__keyspace@{gnoi_shutdown_daemon.STATE_DB_INDEX}__:CHASSIS_MODULE_TABLE|DPU0",
    "data": "set",
}
mock_transition_entry = {
    "state_transition_in_progress": "True",
    "transition_type": "shutdown",
    "pre_shutdown_complete": "True"
}
mock_ip_entry = {"ips@": "10.0.0.1"}
mock_port_entry = {"gnmi_port": "12345"}


class TestGnoiShutdownDaemon(unittest.TestCase):

    def setUp(self):
        # Ensure a clean state for each test
        gnoi_shutdown_daemon.main = gnoi_shutdown_daemon.__dict__["main"]

    def test_execute_gnoi_command_success(self):
        """Test successful execution of a gNOI command."""
        with patch("gnoi_shutdown_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "success")
            self.assertEqual(stderr, "")

    def test_execute_gnoi_command_timeout(self):
        """Test gNOI command timeout."""
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60)):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertIn("Command timed out", stderr)

    def test_execute_gnoi_command_exception(self):
        """Test gNOI command failure due to an exception."""
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=Exception("Test error")):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -2)
            self.assertEqual(stdout, "")
            self.assertIn("Command failed: Test error", stderr)

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.GnoiRebootHandler')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.ModuleBase')
    def test_main_loop_flow(self, mock_module_base, mock_get_pubsub, mock_gnoi_reboot_handler, mock_db_connect):
        """Test the main loop processing of a shutdown event."""
        # Mock DB connections
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

        # Mock pubsub
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt] # Stop after one message
        mock_get_pubsub.return_value = mock_pubsub

        # Mock ModuleBase
        mock_module_base_instance = mock_module_base.return_value
        mock_module_base_instance.get_module_state_transition.return_value = mock_transition_entry

        with self.assertRaises(KeyboardInterrupt):
            gnoi_shutdown_daemon.main()

        # Verify initialization
        mock_db_connect.assert_has_calls([call("STATE_DB"), call("CONFIG_DB")])
        mock_gnoi_reboot_handler.assert_called_with(mock_state_db, mock_config_db, mock_module_base_instance)

        # Verify event handling
        mock_handler_instance = mock_gnoi_reboot_handler.return_value
        mock_handler_instance.handle_transition.assert_called_with("DPU0", "shutdown")

    @patch('gnoi_shutdown_daemon.is_tcp_open', return_value=True)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.execute_gnoi_command')
    def test_handle_transition_success(self, mock_execute_gnoi, mock_get_gnmi_port, mock_get_dpu_ip, mock_is_tcp_open):
        """Test the full successful transition handling."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_mb = MagicMock()

        # Mock return values
        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"
        mock_mb._get_module_gnoi_halt_in_progress.return_value = True
        # Reboot command success, RebootStatus success
        mock_execute_gnoi.side_effect = [
            (0, "reboot sent", ""),
            (0, "reboot complete", "")
        ]

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_mb)
        result = handler.handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_mb._get_module_gnoi_halt_in_progress.assert_called_with("DPU0")
        mock_mb._clear_module_gnoi_halt_in_progress.assert_called_with("DPU0")
        mock_db.hset.assert_has_calls([
            call(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "False"),
            call(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "True")
        ])
        self.assertEqual(mock_execute_gnoi.call_count, 2) # Reboot and RebootStatus

    @patch('gnoi_shutdown_daemon.is_tcp_open', return_value=True)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    def test_handle_transition_gnoi_halt_timeout(self, mock_get_gnmi_port, mock_get_dpu_ip, mock_is_tcp_open):
        """Test transition failure due to gnoi halt in progress timeout."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_mb = MagicMock()

        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"
        # Simulate _get_module_gnoi_halt_in_progress never becoming true
        mock_mb._get_module_gnoi_halt_in_progress.return_value = False

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_mb)

        with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1, 2, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1]):
             result = handler.handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        # Ensure gnoi_shutdown_complete is set to False at the beginning and not set to True
        mock_db.hset.assert_called_once_with(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "False")

    def test_get_dpu_ip_and_port(self):
        """Test DPU IP and gNMI port retrieval."""
        mock_config_db = MagicMock()

        # Test IP retrieval
        mock_config_db.get_entry.return_value = mock_ip_entry
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config_db, "DPU0")
        self.assertEqual(ip, "10.0.0.1")
        mock_config_db.get_entry.assert_called_with("DHCP_SERVER_IPV4_PORT", "bridge-midplane|dpu0")

        # Test port retrieval
        mock_config_db.get_entry.return_value = mock_port_entry
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config_db, "DPU0")
        self.assertEqual(port, "12345")
        mock_config_db.get_entry.assert_called_with("DPU_PORT", "DPU0")

        # Test port fallback
        mock_config_db.get_entry.return_value = {}
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config_db, "DPU0")
        self.assertEqual(port, "8080")

if __name__ == '__main__':
    unittest.main()
