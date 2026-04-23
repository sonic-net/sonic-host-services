import unittest
from unittest.mock import patch, MagicMock, call
import subprocess
import sys
import os
import json

# Mock SONiC and system modules not available in test environment
sys.modules['redis'] = MagicMock()
sys.modules['sonic_py_common'] = MagicMock()
sys.modules['sonic_py_common.daemon_base'] = MagicMock()
sys.modules['sonic_py_common.syslogger'] = MagicMock()
sys.modules['swsscommon'] = MagicMock()
sys.modules['swsscommon.swsscommon'] = MagicMock()

# Mock grpc before importing daemon
mock_grpc = MagicMock()
mock_grpc.RpcError = type('RpcError', (Exception,), {})
sys.modules['grpc'] = mock_grpc

# Mock the gnoi proto stubs
mock_system_pb2 = MagicMock()
mock_system_pb2.HALT = 3
mock_system_pb2.RebootRequest = MagicMock()
mock_system_pb2.RebootStatusRequest = MagicMock()
mock_status_enum = MagicMock()
mock_status_enum.STATUS_SUCCESS = 1
mock_system_pb2.RebootStatus.Status = mock_status_enum

mock_system_pb2_grpc = MagicMock()
sys.modules['host_modules'] = MagicMock()
sys.modules['host_modules.gnoi'] = MagicMock()
sys.modules['host_modules.gnoi.client'] = MagicMock()
sys.modules['host_modules.gnoi.system_pb2'] = mock_system_pb2
sys.modules['host_modules.gnoi.system_pb2_grpc'] = mock_system_pb2_grpc

# Mock sonic_platform_base for ModuleBase constants
mock_module_base = MagicMock()
mock_module_base.MODULE_STATUS_ONLINE = "Online"
mock_module_base.MODULE_STATUS_OFFLINE = "Offline"
mock_module_base.MODULE_STATUS_POWERED_DOWN = "PoweredDown"
mock_module_base.MODULE_STATUS_FAULT = "Fault"
mock_platform_base = MagicMock()
mock_platform_base.module_base.ModuleBase = mock_module_base
sys.modules['sonic_platform_base'] = mock_platform_base
sys.modules['sonic_platform_base.module_base'] = mock_platform_base.module_base

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

import gnoi_shutdown_daemon

# Use our mock ModuleBase
ModuleBase = mock_module_base

# Patch the module-level references after import
gnoi_shutdown_daemon.grpc = mock_grpc
gnoi_shutdown_daemon.system_pb2 = mock_system_pb2

# Common fixtures
mock_message = {
    "type": "pmessage",
    "channel": f"__keyspace@{gnoi_shutdown_daemon.CONFIG_DB_INDEX}__:CHASSIS_MODULE|DPU0",
    "data": "hset",
}


def _make_grpc_client_mock(reboot_status_resp=None, reboot_side_effect=None):
    """Helper to create a mock GnoiClient context manager."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    # All service RPCs go through client.system.*
    system_stub = MagicMock()
    client.system = system_stub
    if reboot_side_effect:
        system_stub.Reboot.side_effect = reboot_side_effect
    if reboot_status_resp is not None:
        system_stub.RebootStatus.return_value = reboot_status_resp
    return client


class TestGnoiShutdownDaemon(unittest.TestCase):

    def setUp(self):
        gnoi_shutdown_daemon.main = gnoi_shutdown_daemon.__dict__["main"]

    # ---- execute_command (still used for non-gNOI commands) ----

    def test_execute_command_success(self):
        with patch("gnoi_shutdown_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "success")

    def test_execute_command_timeout(self):
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60)):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertIn("Command timed out", stderr)

    def test_execute_command_exception(self):
        with patch("gnoi_shutdown_daemon.subprocess.run", side_effect=Exception("Test error")):
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_command(["dummy"])
            self.assertEqual(rc, -2)
            self.assertIn("Command failed: Test error", stderr)

    # ---- _get_halt_timeout ----

    def test_get_halt_timeout_from_platform_json(self):
        from unittest.mock import mock_open
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test_platform"
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis
        mock_platform_class = MagicMock(return_value=mock_platform_instance)
        mock_platform_module = MagicMock()
        mock_platform_module.Platform = mock_platform_class

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': mock_platform_module}):
            with patch("gnoi_shutdown_daemon.os.path.exists", return_value=True):
                with patch("builtins.open", mock_open(read_data=json.dumps({"dpu_halt_services_timeout": 120}))):
                    self.assertEqual(gnoi_shutdown_daemon._get_halt_timeout(), 120)

    def test_get_halt_timeout_default(self):
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test_platform"
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis
        mock_platform_class = MagicMock(return_value=mock_platform_instance)
        mock_platform_module = MagicMock()
        mock_platform_module.Platform = mock_platform_class

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': mock_platform_module}):
            with patch("gnoi_shutdown_daemon.os.path.exists", return_value=False):
                self.assertEqual(gnoi_shutdown_daemon._get_halt_timeout(), gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC)

    def test_get_halt_timeout_exception(self):
        mock_chassis = MagicMock()
        mock_chassis.get_name.return_value = "test-platform"
        mock_platform_class = MagicMock()
        mock_platform_class.return_value.get_chassis.return_value = mock_chassis

        with patch.dict('sys.modules', {'sonic_platform': MagicMock(), 'sonic_platform.platform': MagicMock(Platform=mock_platform_class)}), \
             patch('gnoi_shutdown_daemon.open', side_effect=OSError("File system error")):
            self.assertEqual(gnoi_shutdown_daemon._get_halt_timeout(), gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC)

    # ---- Main loop ----

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.GnoiRebootHandler')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    @patch('threading.Thread')
    def test_main_loop_flow(self, mock_thread, mock_config_db_connector_class, mock_gnoi_reboot_handler, mock_db_connect):
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]
        mock_config_db.hget.return_value = "down"

        mock_config_db_connector = MagicMock()
        mock_config_db_connector.db_name = "CONFIG_DB"
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_redis_client = MagicMock()
        mock_redis_client.pubsub.return_value = mock_pubsub
        mock_config_db_connector.get_redis_client.return_value = mock_redis_client
        mock_config_db_connector_class.return_value = mock_config_db_connector

        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

        mock_db_connect.assert_has_calls([call("STATE_DB"), call("CONFIG_DB")])
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    def test_main_loop_no_dpu_name(self, mock_config_db_connector_class, mock_db_connect):
        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        mock_pubsub = MagicMock()
        malformed_message = mock_message.copy()
        malformed_message["channel"] = f"__keyspace@{gnoi_shutdown_daemon.CONFIG_DB_INDEX}__:CHASSIS_MODULE|"
        mock_pubsub.get_message.side_effect = [malformed_message, KeyboardInterrupt]

        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]

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
        mock_chassis = MagicMock()
        mock_platform_instance = MagicMock()
        mock_platform_instance.get_chassis.return_value = mock_chassis
        mock_platform_submodule = MagicMock()
        mock_platform_submodule.Platform.return_value = mock_platform_instance
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]

        mock_config_db = MagicMock()
        mock_state_db = MagicMock()
        mock_db_connect.side_effect = [mock_state_db, mock_config_db]
        mock_config_db.hget.side_effect = AttributeError("DB error")

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

    # ---- DPU IP / Port helpers ----

    def test_get_dpu_ip_and_port(self):
        mock_config = MagicMock()
        mock_config.hget.return_value = "10.0.0.1"
        ip = gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU0")
        self.assertEqual(ip, "10.0.0.1")
        mock_config.hget.assert_called_with("DHCP_SERVER_IPV4_PORT|bridge-midplane|dpu0", "ips@")

        mock_config = MagicMock()
        mock_config.hget.return_value = "12345"
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")

        mock_config = MagicMock()
        mock_config.hget.return_value = None
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "8080")

    def test_get_dpu_ip_with_string_ips(self):
        mock_config = MagicMock()
        mock_config.hget.return_value = "10.0.0.5"
        self.assertEqual(gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1"), "10.0.0.5")

    def test_get_dpu_ip_empty_entry(self):
        mock_config = MagicMock()
        mock_config.hget.return_value = None
        self.assertIsNone(gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1"))

    def test_get_dpu_ip_no_ips_field(self):
        mock_config = MagicMock()
        mock_config.hget.return_value = None
        self.assertIsNone(gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1"))

    def test_get_dpu_ip_exception(self):
        mock_config = MagicMock()
        mock_config.hget.side_effect = AttributeError("Database error")
        self.assertIsNone(gnoi_shutdown_daemon.get_dpu_ip(mock_config, "DPU1"))

    def test_get_dpu_gnmi_port_exception(self):
        mock_config = MagicMock()
        mock_config.hget.side_effect = AttributeError("Database error")
        self.assertEqual(gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU1"), "8080")

    def test_get_dpu_gnmi_port_variants(self):
        mock_config = MagicMock()
        mock_config.hget.side_effect = [None, None, "12345"]
        port = gnoi_shutdown_daemon.get_dpu_gnmi_port(mock_config, "DPU0")
        self.assertEqual(port, "12345")
        self.assertEqual(mock_config.hget.call_count, 3)

    # ---- gRPC: _send_reboot_command ----

    @patch('gnoi_shutdown_daemon.GnoiClient')
    def test_send_reboot_command_success(self, mock_gnoi_client_class):
        mock_client = _make_grpc_client_mock()
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertTrue(handler._send_reboot_command("DPU0", "10.0.0.1", "8080"))
        mock_client.system.Reboot.assert_called_once()

    @patch('gnoi_shutdown_daemon.GnoiClient')
    def test_send_reboot_command_grpc_error(self, mock_gnoi_client_class):
        rpc_error = mock_grpc.RpcError()
        rpc_error.code = MagicMock(return_value="UNAVAILABLE")
        rpc_error.details = MagicMock(return_value="connection refused")

        mock_client = _make_grpc_client_mock(reboot_side_effect=rpc_error)
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertFalse(handler._send_reboot_command("DPU0", "10.0.0.1", "8080"))

    # ---- gRPC: _poll_reboot_status ----

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_poll_reboot_status_success(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, _):
        mock_monotonic.side_effect = [0, 1]
        mock_resp = MagicMock()
        mock_resp.active = False
        mock_resp.status.status = mock_status_enum.STATUS_SUCCESS

        mock_client = _make_grpc_client_mock(reboot_status_resp=mock_resp)
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertTrue(handler._poll_reboot_status("DPU0", "10.0.0.1", "8080"))

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_poll_reboot_status_timeout(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, _):
        mock_monotonic.side_effect = [0, 1, 61]
        mock_resp = MagicMock()
        mock_resp.active = True

        mock_client = _make_grpc_client_mock(reboot_status_resp=mock_resp)
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertFalse(handler._poll_reboot_status("DPU0", "10.0.0.1", "8080"))

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_poll_reboot_status_failure_status(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, _):
        mock_monotonic.side_effect = [0, 1]
        mock_resp = MagicMock()
        mock_resp.active = False
        mock_resp.status.status = 2  # not STATUS_SUCCESS
        mock_resp.status.message = "internal error"

        mock_client = _make_grpc_client_mock(reboot_status_resp=mock_resp)
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertFalse(handler._poll_reboot_status("DPU0", "10.0.0.1", "8080"))

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_poll_reboot_status_rpc_error_recovery(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, _):
        mock_monotonic.side_effect = [0, 1, 2]

        rpc_error = mock_grpc.RpcError()
        rpc_error.code = MagicMock(return_value="UNAVAILABLE")
        rpc_error.details = MagicMock(return_value="transient")

        mock_resp = MagicMock()
        mock_resp.active = False
        mock_resp.status.status = mock_status_enum.STATUS_SUCCESS

        mock_client = _make_grpc_client_mock()
        mock_client.system.RebootStatus.side_effect = [rpc_error, mock_resp]
        mock_gnoi_client_class.return_value = mock_client

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        self.assertTrue(handler._poll_reboot_status("DPU0", "10.0.0.1", "8080"))

    # ---- _handle_transition ----

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_handle_transition_success(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, mock_get_gnmi_port, mock_get_dpu_ip, _):
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"

        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "True")])

        mock_monotonic.side_effect = [0, 1, 2, 3]

        mock_reboot_client = _make_grpc_client_mock()
        mock_status_resp = MagicMock()
        mock_status_resp.active = False
        mock_status_resp.status.status = mock_status_enum.STATUS_SUCCESS
        mock_status_client = _make_grpc_client_mock(reboot_status_resp=mock_status_resp)
        mock_gnoi_client_class.side_effect = [mock_reboot_client, mock_status_client]

        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_reboot_client.system.Reboot.assert_called_once()
        mock_status_client.system.RebootStatus.assert_called_once()
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port')
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_handle_transition_gnoi_halt_timeout(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, mock_get_gnmi_port, mock_get_dpu_ip, _):
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = "8080"

        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "False")])

        mock_monotonic.side_effect = [
            0, 1, 2, gnoi_shutdown_daemon.STATUS_POLL_TIMEOUT_SEC + 1,
            0, 1
        ]

        mock_reboot_client = _make_grpc_client_mock()
        mock_status_resp = MagicMock()
        mock_status_resp.active = False
        mock_status_resp.status.status = mock_status_enum.STATUS_SUCCESS
        mock_status_client = _make_grpc_client_mock(reboot_status_resp=mock_status_resp)
        mock_gnoi_client_class.side_effect = [mock_reboot_client, mock_status_client]

        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value=None)
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    def test_handle_transition_ip_failure(self, mock_get_gnmi_port, mock_get_dpu_ip, _):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)

        self.assertFalse(handler._handle_transition("DPU0", "shutdown"))
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', side_effect=Exception("Port lookup failed"))
    def test_handle_transition_config_exception(self, mock_get_port, mock_get_ip, _):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)

        self.assertFalse(handler._handle_transition("DPU0", "shutdown"))
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    # ---- _should_skip_gnoi_shutdown (from upstream #352) ----

    def test_should_skip_gnoi_shutdown_offline(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertTrue(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_powered_down(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_POWERED_DOWN
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertTrue(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_online(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertFalse(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_fault(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_FAULT
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertFalse(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_bad_index(self):
        mock_chassis = MagicMock()
        mock_chassis.get_module_index.return_value = -1

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertIsNone(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_no_module(self):
        mock_chassis = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = None

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertIsNone(handler._should_skip_gnoi_shutdown("DPU0"))

    # ---- _handle_transition with oper_status skip (from upstream #352) ----

    def test_handle_transition_dpu_already_offline(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        result = handler._handle_transition("DPU0", "shutdown")
        self.assertTrue(result)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_handle_transition_dpu_powered_down(self):
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_POWERED_DOWN
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        result = handler._handle_transition("DPU0", "shutdown")
        self.assertTrue(result)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_port', return_value="8080")
    @patch('gnoi_shutdown_daemon.GnoiClient')
    @patch('gnoi_shutdown_daemon.time.sleep')
    @patch('gnoi_shutdown_daemon.time.monotonic')
    def test_handle_transition_dpu_fault_proceeds(self, mock_monotonic, mock_sleep, mock_gnoi_client_class, mock_port, mock_ip, _):
        """DPU in Fault state should still attempt gNOI shutdown."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_FAULT
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "True")])

        mock_monotonic.side_effect = [0, 1, 2, 3]

        mock_reboot_client = _make_grpc_client_mock()
        mock_status_resp = MagicMock()
        mock_status_resp.active = False
        mock_status_resp.status.status = mock_status_enum.STATUS_SUCCESS
        mock_status_client = _make_grpc_client_mock(reboot_status_resp=mock_status_resp)
        mock_gnoi_client_class.side_effect = [mock_reboot_client, mock_status_client]

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
            result = handler._handle_transition("DPU0", "shutdown")

        self.assertTrue(result)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_handle_transition_dpu_offline_clear_halt_failure(self):
        """Test clear_halt_flag failure path when DPU is already offline."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_module.clear_module_gnoi_halt_in_progress.side_effect = Exception("platform error")
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        result = handler._handle_transition("DPU0", "shutdown")
        # When skip=True but clear_halt_flag fails, returns False (= cleared failed)
        self.assertFalse(result)

    def test_handle_transition_oper_status_check_exception(self):
        """Test that transition proceeds when oper status check raises exception."""
        mock_chassis = MagicMock()
        mock_chassis.get_module_index.side_effect = Exception("chassis error")

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        # Mock remaining methods to prevent actual execution
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        handler._send_reboot_command = MagicMock(return_value=False)
        handler._clear_halt_flag = MagicMock(return_value=True)

        # Should proceed with transition despite oper check failure
        result = handler._handle_transition("DPU0", "shutdown")
        # Transition fails because send_reboot returns False
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
