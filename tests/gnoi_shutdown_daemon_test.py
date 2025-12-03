import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import sys
import os
import json

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

    def test_execute_command_success(self):
        """Test successful execution of a gNOI command."""
        with patch("gnoi_shutdown_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "success")
            self.assertEqual(stderr, "")

    def test_execute_command_timeout(self):
        """Test gNOI command timeout."""
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60)):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertIn("Command timed out", stderr)

    def test_execute_command_exception(self):
        """Test gNOI command failure due to an exception."""
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=Exception("Test error")):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, -2)
            self.assertEqual(stdout, "")
            self.assertIn("Command failed: Test error", stderr)

    def test_get_halt_timeout_from_platform_json(self):
        """Test _get_halt_timeout with platform.json containing timeout."""
        from unittest.mock import mock_open

        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test_platform"

        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        mock_platform_class = MagicMock(return_value=mock_platform_instance)
        mock_platform_module = MagicMock()
        mock_platform_module.Platform = mock_platform_class

        platform_json_content = {"dpu_halt_services_timeout": 120}

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': mock_platform_module}):
            with patch("gnoi_shutdown_daemon.os.path.exists", return_value=True):
                with patch("builtins.open", mock_open(read_data=json.dumps(platform_json_content))):
                    timeout = gnoi_shutdown_daemon._get_halt_timeout()
                    self.assertEqual(timeout, 120)

    def test_get_halt_timeout_default(self):
        """Test _get_halt_timeout returns default when platform.json not found."""
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test_platform"

        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis

        mock_platform_class = MagicMock(return_value=mock_platform_instance)
        mock_platform_module = MagicMock()
        mock_platform_module.Platform = mock_platform_class

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': mock_platform_module}):
            with patch("gnoi_shutdown_daemon.os.path.exists", return_value=False):
                timeout = gnoi_shutdown_daemon._get_halt_timeout()
                self.assertEqual(timeout, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC)

    def test_get_halt_timeout_exception(self):
        """Test _get_halt_timeout returns default on exception."""
        # Mock sonic_platform import to succeed, then mock file operation to raise exception
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test-platform"
        mock_platform_class = MagicMock()
        mock_platform_class.return_value.get_chassis.return_value = mock_chassis

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': MagicMock(Platform=mock_platform_class)}), \
             patch('gnoi_shutdown_daemon.open', side_effect=OSError("File system error")):
            timeout = gnoi_shutdown_daemon._get_halt_timeout()
            self.assertEqual(timeout, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC)

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.GnoiRebootHandler')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    @patch('threading.Thread')
    def test_main_loop_flow(self, mock_thread, mock_config_db_connector_class, mock_gnoi_reboot_handler, mock_db_connect):
        """Test the main loop processing of a shutdown event."""
        # Mock DB connections
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

        # Mock config_db.hget to return admin_status=down to trigger thread creation
        mock_config_db.hget.return_value = "down"

        # Mock ConfigDBConnector for pubsub
        mock_config_db_connector = MagicMock()
        mock_config_db_connector.db_name = "CONFIG_DB"
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_redis_client = MagicMock()
        mock_redis_client.pubsub.return_value = mock_pubsub
        mock_config_db_connector.get_redis_client.return_value = mock_redis_client
        mock_config_db_connector_class.return_value = mock_config_db_connector

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

        # Mock the reboot handler's _handle_transition to avoid actual execution
        mock_handler_instance = MagicMock()
        mock_gnoi_reboot_handler.return_value = mock_handler_instance

        # Temporarily add mocks to sys.modules for the duration of this test
        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

        # Verify initialization
        mock_db_connect.assert_has_calls([call("STATE_DB"), call("CONFIG_DB")])
        mock_gnoi_reboot_handler.assert_called_with(mock_state_db, mock_config_db, mock_chassis)

        # Verify that a thread was created to handle the transition
        mock_thread.assert_called_once()
        # Verify the thread was started
        mock_thread.return_value.start.assert_called_once()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.execute_command')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_handle_transition_success(self, mock_monotonic, mock_sleep, mock_execute_command, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
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
        mock_execute_command.side_effect = [
            (0, "reboot sent", ""),
            (0, "reboot complete", "")
        ]

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_chassis.get_module_index.assert_called_with("DPU0")
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()
        self.assertEqual(mock_execute_command.call_count, 2)

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    @patch('gnoi_shutdown_daemon.execute_command')
    def test_handle_transition_gnoi_halt_timeout(self, mock_execute_command, mock_monotonic, mock_sleep, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
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
        mock_execute_command.side_effect = [
            (0, "reboot sent", ""),
            (0, "reboot complete", "")
        ]

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        # Should still succeed - code proceeds anyway after timeout warning
        self.assertTrue(result)
        mock_chassis.get_module_index.assert_called_with("DPU0")
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_get_dpu_ip_and_port(self):
        """Test DPU IP and gNMI port retrieval."""
        # Test IP retrieval
        mock_config = MagicMock()
        mock_config.hget.return_value = "10.0.0.1"

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU0")
        self.assertEqual(ip, "10.0.0.1")
        mock_config.hget.assert_called_with("DHCP_SERVER_IPV4_PORT|bridge-midplane|dpu0", "ips@")

        # Test port retrieval
        mock_config = MagicMock()
        mock_config.hget.return_value = "12345"

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")

        # Test port fallback
        mock_config = MagicMock()
        mock_config.hget.return_value = None

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "8080")

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value=None)
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    def test_handle_transition_ip_failure(self, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
        """Test handle_transition failure on DPU IP retrieval."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        
        # Mock _wait_for_gnoi_halt_in_progress to return immediately to prevent hanging
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        
        result = handler._handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        # Verify that clear_module_gnoi_halt_in_progress was called
        mock_chassis.get_module_index.assert_called_with("DPU0")
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.execute_command', return_value=(-1, "", "error"))
    def test_send_reboot_command_failure(self, mock_execute, mock_get_port, mock_get_ip):
        """Test failure of _send_reboot_command."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
        self.assertFalse(result)

    def test_get_dpu_gnmi_port_variants(self):
        """Test DPU gNMI port retrieval with name variants."""
        mock_config = MagicMock()
        mock_config.hget.side_effect = [
            None,  # dpu0 fails
            None,  # DPU0 fails
            "12345"  # DPU0 succeeds
        ]

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")
        self.assertEqual(mock_config.hget.call_count, 3)

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    def test_main_loop_no_dpu_name(self, mock_config_db_connector_class, mock_db_connect):
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

        # Mock DB connections
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

        # Mock ConfigDBConnector for pubsub
        mock_config_db_connector = MagicMock()
        mock_config_db_connector.db_name = "CONFIG_DB"
        mock_redis_client = MagicMock()
        mock_redis_client.pubsub.return_value = mock_pubsub
        mock_config_db_connector.get_redis_client.return_value = mock_redis_client
        mock_config_db_connector_class.return_value = mock_config_db_connector

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    def test_main_loop_get_transition_exception(self, mock_config_db_connector_class, mock_db_connect):
        """Test main loop when hget raises an exception."""
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

        # Mock config_db to raise exception on hget
        mock_config_db = MagicMock()
        mock_state_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]
        mock_config_db.hget.side_effect = AttributeError("DB error")

        # Mock ConfigDBConnector for pubsub
        mock_config_db_connector = MagicMock()
        mock_config_db_connector.db_name = "CONFIG_DB"
        mock_redis_client = MagicMock()
        mock_redis_client.pubsub.return_value = mock_pubsub
        mock_config_db_connector.get_redis_client.return_value = mock_redis_client
        mock_config_db_connector_class.return_value = mock_config_db_connector

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.execute_command', return_value=(-1, "", "RPC error"))
    def test_poll_reboot_status_failure(self, mock_execute_command, mock_get_halt_timeout):
        """Test _poll_reboot_status with a command failure."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1, 61]):
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

            # Verify it worked
            self.assertEqual(chassis, mock_chassis)
            self.assertEqual(chassis.get_name(), "test_chassis")
            mock_platform_class.assert_called_once()
            mock_platform_instance.get_chassis.assert_called_once()

    def test_get_dpu_ip_with_string_ips(self):
        """Test get_dpu_ip when ips is a string instead of list."""
        mock_config = MagicMock()
        mock_config.hget.return_value = "10.0.0.5"

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertEqual(ip, "10.0.0.5")

    def test_get_dpu_ip_empty_entry(self):
        """Test get_dpu_ip when entry is empty."""
        mock_config = MagicMock()
        mock_config.hget.return_value = None

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_ip_no_ips_field(self):
        """Test get_dpu_ip when hget returns None (field doesn't exist)."""
        mock_config = MagicMock()
        mock_config.hget.return_value = None

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_ip_exception(self):
        """Test get_dpu_ip when exception occurs."""
        mock_config = MagicMock()
        mock_config.hget.side_effect = AttributeError("Database error")

        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1")
        self.assertIsNone(ip)

    def test_get_dpu_gnmi_port_exception(self):
        """Test get_dpu_gnmi_port when exception occurs."""
        mock_config = MagicMock()
        mock_config.hget.side_effect = AttributeError("Database error")

        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU1")
        self.assertEqual(port, "8080")

    def test_send_reboot_command_success(self):
        """Test successful _send_reboot_command."""
        with patch('gnoi_shutdown_daemon.execute_command', return_value=(0, "success", "")):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
            result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
            self.assertTrue(result)

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', side_effect=Exception("Port lookup failed"))
    def test_handle_transition_config_exception(self, mock_get_port, mock_get_ip, mock_get_halt_timeout):
        """Test handle_transition when configuration lookup raises exception."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        
        # Mock _wait_for_gnoi_halt_in_progress to return immediately to prevent hanging
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        
        result = handler._handle_transition("DPU0", "shutdown")

        self.assertFalse(result)
        # Verify that clear_module_gnoi_halt_in_progress was called
        mock_chassis.get_module_index.assert_called_with("DPU0")
        mock_chassis.get_module.assert_called_with(0)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()


if __name__ == '__main__':
    unittest.main()
