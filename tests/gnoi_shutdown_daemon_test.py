import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

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
        # Ensure gnoi_shutdown_complete is set to False
        mock_db.hset.assert_called_with(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "False")

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

    def test_get_pubsub_fallback(self):
        """Test _get_pubsub fallback to raw redis client."""
        mock_db = MagicMock()
        # Simulate connector without a direct pubsub() method
        del mock_db.pubsub
        mock_redis_client = MagicMock()
        mock_db.get_redis_client.return_value = mock_redis_client

        pubsub = gnoi_shutdown_daemon._get_pubsub(mock_db)

        mock_db.get_redis_client.assert_called_with(mock_db.STATE_DB)
        self.assertEqual(pubsub, mock_redis_client.pubsub.return_value)

    @patch('gnoi_shutdown_daemon.is_tcp_open', return_value=False)
    def test_handle_transition_unreachable(self, mock_is_tcp_open):
        """Test handle_transition when DPU is unreachable."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_mb = MagicMock()
        mock_get_dpu_ip = patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1").start()
        mock_get_dpu_gnmi_port = patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080").start()

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_mb)
        result = handler.handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        mock_is_tcp_open.assert_called_with("10.0.0.1", 8080)
        # Called twice: once at the start, once on this failure path
        self.assertEqual(mock_db.hset.call_count, 2)
        mock_db.hset.assert_called_with(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "False")

        patch.stopall()

    @patch('gnoi_shutdown_daemon.is_tcp_open', return_value=True)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', side_effect=RuntimeError("IP not found"))
    def test_handle_transition_ip_failure(self, mock_get_dpu_ip, mock_is_tcp_open):
        """Test handle_transition failure on DPU IP retrieval."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_mb = MagicMock()

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_mb)
        result = handler.handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        self.assertEqual(mock_db.hset.call_count, 2)
        mock_db.hset.assert_called_with(mock_db.STATE_DB, "CHASSIS_MODULE_TABLE|DPU0", "gnoi_shutdown_complete", "False")

    @patch('gnoi_shutdown_daemon.is_tcp_open', return_value=True)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.execute_gnoi_command', return_value=(-1, "", "error"))
    def test_send_reboot_command_failure(self, mock_execute, mock_get_port, mock_get_ip, mock_is_tcp_open):
        """Test failure of _send_reboot_command."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
        self.assertFalse(result)

    def test_set_gnoi_shutdown_flag_exception(self):
        """Test exception handling in _set_gnoi_shutdown_complete_flag."""
        mock_db = MagicMock()
        mock_db.hset.side_effect = Exception("Redis error")
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, MagicMock(), MagicMock())
        # We don't expect an exception to be raised, just logged.
        handler._set_gnoi_shutdown_complete_flag("DPU0", True)
        mock_db.hset.assert_called_once()

    def test_is_tcp_open_os_error(self):
        """Test is_tcp_open with an OSError."""
        with patch('gnoi_shutdown_daemon.socket.create_connection', side_effect=OSError):
            self.assertFalse(gnoi_shutdown_daemon.is_tcp_open("localhost", 1234))

    def test_get_dpu_gnmi_port_variants(self):
        """Test DPU gNMI port retrieval with name variants."""
        mock_config_db = MagicMock()
        mock_config_db.get_entry.side_effect = [
            {},  # DPU0 fails
            {},  # dpu0 fails
            mock_port_entry  # DPU0 succeeds
        ]
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config_db, "DPU0")
        self.assertEqual(port, "12345")
        mock_config_db.get_entry.assert_has_calls([
            call("DPU_PORT", "DPU0"),
            call("DPU_PORT", "dpu0"),
            call("DPU_PORT", "DPU0")
        ])

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.ModuleBase')
    def test_main_loop_no_dpu_name(self, mock_module_base, mock_get_pubsub, mock_db_connect):
        """Test main loop with a malformed key."""
        mock_pubsub = MagicMock()
        # Malformed message, then stop
        malformed_message = mock_message.copy()
        malformed_message["channel"] = f"__keyspace@{gnoi_shutdown_daemon.STATE_DB_INDEX}__:CHASSIS_MODULE_TABLE|"
        mock_pubsub.get_message.side_effect = [malformed_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        with self.assertRaises(KeyboardInterrupt):
            gnoi_shutdown_daemon.main()
        # Ensure get_module_state_transition was never called
        mock_module_base.return_value.get_module_state_transition.assert_not_called()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.ModuleBase')
    def test_main_loop_get_transition_exception(self, mock_module_base, mock_get_pubsub, mock_db_connect):
        """Test main loop when get_module_state_transition raises an exception."""
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub
        mock_module_base.return_value.get_module_state_transition.side_effect = Exception("DB error")

        with self.assertRaises(KeyboardInterrupt):
            gnoi_shutdown_daemon.main()
        mock_module_base.return_value.get_module_state_transition.assert_called_with("DPU0")

    @patch('gnoi_shutdown_daemon.execute_gnoi_command', return_value=(-1, "", "RPC error"))
    def test_poll_reboot_status_failure(self, mock_execute_gnoi):
        """Test _poll_reboot_status with a command failure."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1]):
            result = handler._poll_reboot_status("DPU0", "10.0.0.1", "8080")
        self.assertFalse(result)

if __name__ == '__main__':
    unittest.main()
