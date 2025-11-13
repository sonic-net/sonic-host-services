import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import sys
import os

# Mock redis module (available in SONiC runtime, not in test environment)
sys.modules['redis'] = MagicMock()

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
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    @patch('threading.Thread')
    def test_main_loop_flow(self, mock_thread, mock_config_connector, mock_get_pubsub, mock_gnoi_reboot_handler, mock_db_connect):
        """Test the main loop processing of a shutdown event."""
        # Mock DB connections
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

        # Mock config_db.get_entry to return admin_status=down to trigger thread creation
        mock_config_db.get_entry.return_value = mock_config_entry

        # Mock chassis
        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        # Create mock for sonic_platform.platform module
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance

        # Create mock for sonic_platform parent module
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        # Mock pubsub to yield one message then stop
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        # Mock the reboot handler's _handle_transition to avoid actual execution
        mock_handler_instance = MagicMock()
        mock_gnoi_reboot_handler.return_value = mock_handler_instance

        # Temporarily add mocks to sys.modules for the duration of this test
        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
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
    def test_main_loop_no_dpu_name(self, mock_get_pubsub, mock_db_connect):
        """Test main loop with a malformed key."""
        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        # Create mock for sonic_platform.platform module
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance

        # Create mock for sonic_platform parent module
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        mock_pubsub = MagicMock()
        # Malformed message, then stop
        malformed_message = mock_message.copy()
        malformed_message["channel"] = f"__keyspace@{gnoi_shutdown_daemon.CONFIG_DB_INDEX}__:CHASSIS_MODULE|"
        mock_pubsub.get_message.side_effect = [malformed_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with patch('gnoi_shutdown_daemon.redis.Redis'):
                with self.assertRaises(KeyboardInterrupt):
                    gnoi_shutdown_daemon.main()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon._get_pubsub')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    def test_main_loop_get_transition_exception(self, mock_config_connector, mock_get_pubsub, mock_db_connect):
        """Test main loop when get_entry raises an exception."""
        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        # Create mock for sonic_platform.platform module
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance

        # Create mock for sonic_platform parent module
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_get_pubsub.return_value = mock_pubsub

        # Mock ConfigDBConnector to raise exception
        mock_config = MagicMock()
        mock_config_connector.return_value = mock_config
        mock_config.get_entry.side_effect = Exception("DB error")

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
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

    def test_sonic_platform_import_mock(self):
        """Simple test to verify sonic_platform import mocking works."""
        # Create mock chassis
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test_chassis"

        # Create mock platform instance that returns our chassis
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        # Create mock Platform class
        mock_platform_class = MagicMock(return_value=mock_platform_instance)

        # Create mock for sonic_platform.platform module
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform = mock_platform_class

        # Create mock for sonic_platform parent module
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        # Test that we can mock the import
        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            # Simulate what the actual code does
            from sonic_platform import platform
            chassis = platform.Platform().get_chassis()

            # Verify
            self.assertEqual(chassis, mock_chassis)
            self.assertEqual(chassis.get_name(), "test_chassis")
            mock_platform_class.assert_called_once()
            mock_platform_instance.get_chassis.assert_called_once()

    def test_is_tcp_open_success(self):
        """Test is_tcp_open when connection succeeds."""
        with patch('gnoi_shutdown_daemon.socket.create_connection') as mock_socket:
            mock_socket.return_value.__enter__ = MagicMock()
            mock_socket.return_value.__exit__ = MagicMock()
            result = gnoi_shutdown_daemon.is_tcp_open("10.0.0.1", 8080, timeout=1.0)
            self.assertTrue(result)
            mock_socket.assert_called_once_with(("10.0.0.1", 8080), timeout=1.0)

    def test_is_tcp_open_failure(self):
        """Test is_tcp_open when connection fails."""
        with patch('gnoi_shutdown_daemon.socket.create_connection', side_effect=OSError("Connection refused")):
            result = gnoi_shutdown_daemon.is_tcp_open("10.0.0.1", 8080, timeout=1.0)
            self.assertFalse(result)

    def test_get_dpu_ip_with_string_ips(self):
        """Test get_dpu_ip when ips is a string instead of list."""
        mock_config = MagicMock()
        mock_config.get_entry.return_value = {"ips": "10.0.0.5"}
        
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertEqual(ip, "10.0.0.5")

    def test_get_dpu_ip_empty_entry(self):
        """Test get_dpu_ip when entry is empty."""
        mock_config = MagicMock()
        mock_config.get_entry.return_value = {}
        
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_ip_no_ips_field(self):
        """Test get_dpu_ip when entry has no ips field."""
        mock_config = MagicMock()
        mock_config.get_entry.return_value = {"other_field": "value"}
        
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_ip_exception(self):
        """Test get_dpu_ip when exception occurs."""
        mock_config = MagicMock()
        mock_config.get_entry.side_effect = Exception("Database error")
        
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_gnmi_port_exception(self):
        """Test get_dpu_gnmi_port when exception occurs."""
        mock_config = MagicMock()
        mock_config.get_entry.side_effect = Exception("Database error")
        
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU1")
        self.assertEqual(port, "8080")

    def test_send_reboot_command_success(self):
        """Test successful _send_reboot_command."""
        with patch('gnoi_shutdown_daemon.execute_gnoi_command', return_value=(0, "success", "")):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
            result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
            self.assertTrue(result)

    def test_set_gnoi_shutdown_complete_flag_success(self):
        """Test successful setting of gnoi_shutdown_complete flag."""
        mock_db = MagicMock()
        mock_table = MagicMock()
        
        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, MagicMock(), MagicMock())
            handler._set_gnoi_shutdown_complete_flag("DPU0", True)
            
            # Verify the flag was set correctly
            mock_table.set.assert_called_once()
            call_args = mock_table.set.call_args
            self.assertEqual(call_args[0][0], "DPU0")

    def test_is_tcp_open_default_timeout(self):
        """Test is_tcp_open uses environment variable for default timeout."""
        with patch.dict(os.environ, {"GNOI_DIAL_TIMEOUT": "2.5"}):
            with patch('gnoi_shutdown_daemon.socket.create_connection') as mock_socket:
                mock_socket.return_value.__enter__ = MagicMock()
                mock_socket.return_value.__exit__ = MagicMock()
                result = gnoi_shutdown_daemon.is_tcp_open("10.0.0.1", 8080)
                self.assertTrue(result)
                mock_socket.assert_called_once_with(("10.0.0.1", 8080), timeout=2.5)

    def test_get_dpu_ip_list_ips(self):
        """Test get_dpu_ip when ips is a list (normal case)."""
        mock_config = MagicMock()
        mock_config.get_entry.return_value = {"ips": ["10.0.0.10", "10.0.0.11"]}

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU2")
        self.assertEqual(ip, "10.0.0.10")  # Should return first IP

    def test_get_dpu_gnmi_port_found_first_try(self):
        """Test get_dpu_gnmi_port when port is found on first lookup."""
        mock_config = MagicMock()
        # Return port on first call (lowercase)
        mock_config.get_entry.return_value = {"gnmi_port": "9090"}

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU3")
        self.assertEqual(port, "9090")
        # Should only call once if found on first try
        self.assertEqual(mock_config.get_entry.call_count, 1)

    def test_poll_reboot_status_success(self):
        """Test _poll_reboot_status when reboot completes successfully."""
        with patch('gnoi_shutdown_daemon.execute_gnoi_command') as mock_execute:
            with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1]):
                with patch('gnoi_shutdown_daemon.time.sleep'):
                    # Return "Reboot Complete" message
                    mock_execute.return_value = (0, "System Reboot Complete", "")

                    handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
                    result = handler._poll_reboot_status("DPU0", "10.0.0.1", "8080")

                    self.assertTrue(result)

    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    @patch('gnoi_shutdown_daemon.execute_gnoi_command')
    def test_handle_transition_reboot_not_sent(self, mock_execute, mock_monotonic, mock_sleep, mock_get_port, mock_get_ip):
        """Test _handle_transition when reboot command fails to send."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock table for halt_in_progress
        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "True")])

        # Mock time
        mock_monotonic.side_effect = [0, 1, 2, 3, 4, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1]

        # Reboot command fails, status poll times out
        mock_execute.side_effect = [
            (-1, "", "Connection failed"),  # Reboot command fails
            (0, "rebooting", ""),  # Status poll returns non-complete
        ]

        mock_module = MagicMock()
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        # Should return False because reboot status never completed
        self.assertFalse(result)

    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_wait_for_gnoi_halt_status_false(self, mock_monotonic, mock_sleep):
        """Test _wait_for_gnoi_halt_in_progress when status is False but halt flag is True."""
        mock_db = MagicMock()
        mock_table = MagicMock()

        # First call: status=True, halt=False
        # Second call: timeout
        mock_table.get.side_effect = [
            (True, [("gnoi_halt_in_progress", "False")]),
            (True, [("gnoi_halt_in_progress", "False")])
        ]

        mock_monotonic.side_effect = [0, 1, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1]

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, MagicMock(), MagicMock())
            result = handler._wait_for_gnoi_halt_in_progress("DPU0")

        self.assertFalse(result)

    def test_set_gnoi_shutdown_complete_flag_false(self):
        """Test setting gnoi_shutdown_complete flag to False."""
        mock_db = MagicMock()
        mock_table = MagicMock()

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, MagicMock(), MagicMock())
            handler._set_gnoi_shutdown_complete_flag("DPU0", False)

            # Verify the flag was set to False
            mock_table.set.assert_called_once()
            call_args = mock_table.set.call_args
            # Check that the value contains "False"
            fvs = call_args[0][1]
            self.assertIn("False", str(fvs))

    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    def test_handle_transition_get_ip_exception(self, mock_get_port, mock_get_ip):
        """Test _handle_transition when get_dpu_ip raises an exception."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Make get_dpu_ip raise an exception (simulates lines 130-133)
        mock_get_ip.side_effect = Exception("Network configuration error")
        mock_get_port.return_value = "8080"

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        result = handler._handle_transition("DPU0", "shutdown")

        # Should return False and set completion flag to False
        self.assertFalse(result)

if __name__ == '__main__':
    unittest.main()
