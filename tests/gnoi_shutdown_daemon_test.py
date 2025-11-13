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
    "channel": f"__keyspace@{gnoi_shutdown_daemon.CONFIG_DB_INDEX}__:CHASSIS_MODULE|DPU0",
    "data": "hset",
}
mock_config_entry = {
    "admin_status": "down"
}
mock_ip_entry = {"ips": ["10.0.0.1"]}
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
    @patch('gnoi_shutdown_daemon.sonic_platform.platform')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    @patch('threading.Thread')
    def test_main_loop_flow(self, mock_thread, mock_config_connector, mock_platform, mock_get_pubsub, mock_gnoi_reboot_handler, mock_db_connect):
        """Test the main loop processing of a shutdown event."""
        # Mock DB connections
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

        # Mock chassis
        mock_chassis = MagicMock()
        mock_platform_instance = mock_platform.return_value
        mock_platform_instance.get_chassis.return_value = mock_chassis

        # Mock pubsub to yield one message then stop
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        # Mock ConfigDB to return a valid entry
        mock_config = MagicMock()
        mock_config_connector.return_value = mock_config
        mock_config.get_entry.return_value = mock_config_entry

        # Mock Redis client for keyspace notification config
        with patch('gnoi_shutdown_daemon.redis.Redis'):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

        # Verify initialization
        mock_db_connect.assert_has_calls([call("STATE_DB"), call("CONFIG_DB")])
        mock_gnoi_reboot_handler.assert_called_with(mock_state_db, mock_config_db, mock_chassis)

        # Verify that a thread was created to handle the transition
        mock_thread.assert_called_once()
        # Verify the thread was started
        mock_thread.return_value.start.assert_called_once()

    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.execute_gnoi_command')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_handle_transition_success(self, mock_monotonic, mock_sleep, mock_execute_gnoi, mock_get_gnmi_port, mock_get_dpu_ip):
        """Test the full successful transition handling."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock return values
        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"
        
        # Mock table.get() for gnoi_halt_in_progress check
        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "True")])
        
        # Mock time for polling
        mock_monotonic.side_effect = [
            0, 1,  # For _wait_for_gnoi_halt_in_progress
            2, 3   # For _poll_reboot_status
        ]
        
        # Reboot command success, RebootStatus success
        mock_execute_gnoi.side_effect = [
            (0, "reboot sent", ""),
            (0, "reboot complete", "")
        ]
        
        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()
        self.assertEqual(mock_execute_gnoi.call_count, 2)

    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    @patch('gnoi_shutdown_daemon.execute_gnoi_command')
    def test_handle_transition_gnoi_halt_timeout(self, mock_execute_gnoi, mock_monotonic, mock_sleep, mock_get_gnmi_port, mock_get_dpu_ip):
        """Test transition proceeds despite gnoi_halt_in_progress timeout."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"
        
        # Mock table.get() to never return True (simulates timeout in wait)
        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "False")])
        
        # Simulate timeout in _wait_for_gnoi_halt_in_progress, then success in _poll_reboot_status
        mock_monotonic.side_effect = [
            # _wait_for_gnoi_halt_in_progress times out
            0, 1, 2, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1,
            # _poll_reboot_status succeeds
            0, 1
        ]
        
        # Reboot command and status succeed
        mock_execute_gnoi.side_effect = [
            (0, "reboot sent", ""),
            (0, "reboot complete", "")
        ]
        
        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        # Should still succeed - code proceeds anyway after timeout warning
        self.assertTrue(result)
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_get_dpu_ip_and_port(self):
        """Test DPU IP and gNMI port retrieval."""
        # Test IP retrieval
        mock_config = MagicMock()
        mock_config.get_entry.return_value = mock_ip_entry

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU0")
        self.assertEqual(ip, "10.0.0.1")
        mock_config.get_entry.assert_called_with("DHCP_SERVER_IPV4_PORT", "bridge-midplane|dpu0")

        # Test port retrieval
        mock_config = MagicMock()
        mock_config.get_entry.return_value = mock_port_entry

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")

        # Test port fallback
        mock_config = MagicMock()
        mock_config.get_entry.return_value = {}

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "8080")

    def test_get_pubsub_fallback(self):
        """Test _get_pubsub with redis client."""
        with patch('gnoi_shutdown_daemon.redis.Redis') as mock_redis:
            mock_redis_instance = MagicMock()
            mock_redis.return_value = mock_redis_instance
            
            pubsub = gnoi_shutdown_daemon._get_pubsub(gnoi_shutdown_daemon.CONFIG_DB_INDEX)
            
            mock_redis.assert_called_with(unix_socket_path='/var/run/redis/redis.sock', db=gnoi_shutdown_daemon.CONFIG_DB_INDEX)
            self.assertEqual(pubsub, mock_redis_instance.pubsub.return_value)

    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value=None)
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.GnoiRebootHandler._set_gnoi_shutdown_complete_flag')
    def test_handle_transition_ip_failure(self, mock_set_flag, mock_get_gnmi_port, mock_get_dpu_ip):
        """Test handle_transition failure on DPU IP retrieval."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        result = handler._handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        # Verify that the completion flag was set to False
        mock_set_flag.assert_called_once_with("DPU0", False)

    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.execute_gnoi_command', return_value=(-1, "", "error"))
    def test_send_reboot_command_failure(self, mock_execute, mock_get_port, mock_get_ip):
        """Test failure of _send_reboot_command."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
        self.assertFalse(result)

    def test_set_gnoi_shutdown_flag_exception(self):
        """Test exception handling in _set_gnoi_shutdown_complete_flag."""
        mock_db = MagicMock()
        mock_table = MagicMock()
        mock_table.set.side_effect = Exception("Redis error")
        
        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, MagicMock(), MagicMock())
            # Should not raise an exception, just log
            handler._set_gnoi_shutdown_complete_flag("DPU0", True)
            mock_table.set.assert_called_once()

    def test_get_dpu_gnmi_port_variants(self):
        """Test DPU gNMI port retrieval with name variants."""
        mock_config = MagicMock()
        mock_config.get_entry.side_effect = [
            {},  # dpu0 fails
            {},  # DPU0 fails
            mock_port_entry  # DPU0 succeeds
        ]

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")
        self.assertEqual(mock_config.get_entry.call_count, 3)

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.sonic_platform.platform')
    def test_main_loop_no_dpu_name(self, mock_platform, mock_get_pubsub, mock_db_connect):
        """Test main loop with a malformed key."""
        mock_chassis = MagicMock()
        mock_platform.return_value.get_chassis.return_value = mock_chassis
        
        mock_pubsub = MagicMock()
        # Malformed message, then stop
        malformed_message = mock_message.copy()
        malformed_message["channel"] = f"__keyspace@{gnoi_shutdown_daemon.CONFIG_DB_INDEX}__:CHASSIS_MODULE|"
        mock_pubsub.get_message.side_effect = [malformed_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        with patch('gnoi_shutdown_daemon.redis.Redis'):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.sonic_platform.platform')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    def test_main_loop_get_transition_exception(self, mock_config_connector, mock_platform, mock_get_pubsub, mock_db_connect):
        """Test main loop when get_entry raises an exception."""
        mock_chassis = MagicMock()
        mock_platform.return_value.get_chassis.return_value = mock_chassis
        
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub
        
        # Mock ConfigDBConnector to raise exception
        mock_config = MagicMock()
        mock_config_connector.return_value = mock_config
        mock_config.get_entry.side_effect = Exception("DB error")

        with patch('gnoi_shutdown_daemon.redis.Redis'):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

    @patch('gnoi_shutdown_daemon.execute_gnoi_command', return_value=(-1, "", "RPC error"))
    def test_poll_reboot_status_failure(self, mock_execute_gnoi):
        """Test _poll_reboot_status with a command failure."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1]):
            result = handler._poll_reboot_status("DPU0", "10.0.0.1", "8080")
        self.assertFalse(result)

if __name__ == '__main__':
    unittest.main()
