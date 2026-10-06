import unittest
from unittest.mock import patch, MagicMock, call
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

import grpc

import gnoi_shutdown_daemon
from sonic_grpc.gnoi import system_pb2
from sonic_platform_base.module_base import ModuleBase

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


class _FakeRpcError(grpc.RpcError):
    """Minimal grpc.RpcError stand-in exposing .code()/.details(), for tests
    that don't need a real channel/server round trip."""

    def __init__(self, code=grpc.StatusCode.UNAVAILABLE, details="unavailable"):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


def _endpoint(port="8080"):
    return gnoi_shutdown_daemon.DpuEndpoint(port=port, credentials=MagicMock(name="creds"))


def _mock_gnoi_client(system_time=None, system_reboot=None, system_reboot_status=None):
    """Build a MagicMock standing in for gnoi_shutdown_daemon.GnoiClient: a
    class whose instances are usable as context managers exposing .system
    with the given Time/Reboot/RebootStatus behavior (return value or
    exception instance to raise)."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def _apply(mock_attr, behavior):
        if isinstance(behavior, Exception):
            mock_attr.side_effect = behavior
        elif behavior is not None:
            mock_attr.return_value = behavior

    _apply(client.system.Time, system_time)
    _apply(client.system.Reboot, system_reboot)
    _apply(client.system.RebootStatus, system_reboot_status)

    cls = MagicMock(return_value=client)
    return cls, client


class TestGnoiShutdownDaemon(unittest.TestCase):

    def setUp(self):
        # Ensure a clean state for each test
        gnoi_shutdown_daemon.main = gnoi_shutdown_daemon.__dict__["main"]

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
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports')
    @patch('gnoi_shutdown_daemon._build_dpu_endpoint')
    def test_handle_transition_success(self, mock_build_endpoint, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
        """Test the full successful transition handling."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock return values
        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = ["8080", "50052"]

        # Mock table.get() for gnoi_halt_in_progress check
        mock_table = MagicMock()
        mock_table.get.return_value = (True, [("gnoi_halt_in_progress", "True")])

        # TLS setup succeeds for both candidate ports; the transport-level
        # probe (System.Time) is what fails for 8080 and succeeds for 50052.
        mock_build_endpoint.side_effect = lambda dpu_ip, port, timeout: gnoi_shutdown_daemon.DpuEndpoint(
            port=port, credentials=MagicMock(name=f"creds-{port}"))

        status_resp = system_pb2.RebootStatusResponse()
        status_resp.active = False
        status_resp.status.status = system_pb2.RebootStatus.Status.STATUS_SUCCESS

        def gnoi_client_side_effect(target, credentials=None):
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False
            if target == "10.0.0.1:8080":
                # Configured port probe fails.
                client.system.Time.side_effect = _FakeRpcError()
            else:
                # Native port probe and subsequent operations succeed.
                client.system.Time.return_value = system_pb2.TimeResponse(time=1)
                client.system.Reboot.return_value = system_pb2.RebootResponse()
                client.system.RebootStatus.return_value = status_resp
            return client

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.swsscommon.Table', return_value=mock_table), \
             patch('gnoi_shutdown_daemon.GnoiClient', side_effect=gnoi_client_side_effect) as mock_gnoi_client:
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            result = handler._handle_transition("DPU0")

        self.assertTrue(result)
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()
        # Probe on 8080 (fails), probe on 50052 (succeeds), Reboot on 50052, RebootStatus on 50052.
        self.assertEqual(mock_gnoi_client.call_count, 4)
        targets = [c.args[0] for c in mock_gnoi_client.call_args_list]
        self.assertEqual(targets, ["10.0.0.1:8080", "10.0.0.1:50052", "10.0.0.1:50052", "10.0.0.1:50052"])
        # Every direct-DPU GnoiClient call must carry TLS credentials -- never a bare/plaintext call.
        for c in mock_gnoi_client.call_args_list:
            self.assertIn('credentials', c.kwargs)
            self.assertIsNotNone(c.kwargs['credentials'])

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip')
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports')
    @patch('gnoi_shutdown_daemon._build_dpu_endpoint')
    def test_handle_transition_gnoi_halt_timeout(self, mock_build_endpoint, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
        """Test transition proceeds despite gnoi_halt_in_progress timeout."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        mock_get_dpu_ip.return_value = "10.0.0.1"
        mock_get_gnmi_port.return_value = ["8080", "50052"]

        mock_build_endpoint.return_value = gnoi_shutdown_daemon.DpuEndpoint(port="8080", credentials=MagicMock())

        status_resp = system_pb2.RebootStatusResponse()
        status_resp.active = False
        status_resp.status.status = system_pb2.RebootStatus.Status.STATUS_SUCCESS
        mock_client, mock_client_instance = _mock_gnoi_client(
            system_time=system_pb2.TimeResponse(time=1),
            system_reboot=system_pb2.RebootResponse(),
            system_reboot_status=status_resp,
        )

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        with patch('gnoi_shutdown_daemon.GnoiClient', mock_client):
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
            # gnoi_halt_in_progress never arrives within the wait window --
            # simulate that outcome directly rather than the wait loop's
            # internal timing, which is unrelated to this transport refactor.
            handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=False)
            result = handler._handle_transition("DPU0")

        # Should still succeed - code proceeds anyway after timeout warning
        self.assertTrue(result)
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

        ports = gnoi_shutdown_daemon.get_dpu_gnmi_ports(mock_config, "DPU0")
        self.assertEqual(ports, ["12345", "8080", "50052"])

        # Test port fallback
        mock_config = MagicMock()
        mock_config.hget.return_value = None

        ports = gnoi_shutdown_daemon.get_dpu_gnmi_ports(mock_config, "DPU0")
        self.assertEqual(ports, ["8080", "50052"])

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value=None)
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports', return_value=["8080", "50052"])
    def test_handle_transition_ip_failure(self, mock_get_gnmi_port, mock_get_dpu_ip, mock_get_halt_timeout):
        """Test handle_transition failure on DPU IP retrieval."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)

        # Mock _wait_for_gnoi_halt_in_progress to return immediately to prevent hanging
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)

        result = handler._handle_transition("DPU0")

        self.assertFalse(result)
        # Verify that clear_module_gnoi_halt_in_progress was called
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_send_reboot_command_failure(self):
        """Test failure of _send_reboot_command."""
        endpoint = _endpoint("50052")
        cls, client = _mock_gnoi_client(system_reboot=_FakeRpcError())
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon.GnoiClient', cls):
            result = handler._send_reboot_command("DPU0", "10.0.0.1", endpoint)
        self.assertFalse(result)
        client.system.Reboot.assert_called_once()
        # Must still have gone out over TLS -- a probe/RPC failure never means
        # a plaintext retry.
        self.assertEqual(cls.call_args.kwargs.get('credentials'), endpoint.credentials)

    def test_get_dpu_gnmi_ports_variants(self):
        """Test DPU gNMI port retrieval with name variants."""
        mock_config = MagicMock()
        mock_config.hget.side_effect = [
            None,  # dpu0 fails
            None,  # DPU0 fails
            "12345"  # DPU0 succeeds
        ]

        ports = gnoi_shutdown_daemon.get_dpu_gnmi_ports(mock_config, "DPU0")
        self.assertEqual(ports, ["12345", "8080", "50052"])
        self.assertEqual(mock_config.hget.call_count, 3)

    def test_get_dpu_gnmi_ports_duplicate_configured_port_not_repeated(self):
        """A configured port equal to a common port (8080) appears only
        once in the probe order, i.e. is never probed twice."""
        mock_config = MagicMock()
        mock_config.hget.return_value = "8080"
        ports = gnoi_shutdown_daemon.get_dpu_gnmi_ports(mock_config, "DPU0")
        self.assertEqual(ports, ["8080", "50052"])
        self.assertEqual(ports.count("8080"), 1)

    def test_fetch_dpu_cert_pem_uses_ssl_get_server_certificate(self):
        """Fetching goes through ssl.get_server_certificate, unverified,
        against (dpu_ip, port) -- port passed through as-is, per the
        maintainer-approved pattern (not int()-converted, so a malformed
        configured port fails this candidate rather than raising)."""
        with patch('gnoi_shutdown_daemon.ssl.get_server_certificate', return_value="PEMDATA") as mock_get_cert:
            result = gnoi_shutdown_daemon._fetch_dpu_cert_pem("10.0.0.5", "8080", timeout=7)
        mock_get_cert.assert_called_once_with(("10.0.0.5", "8080"), timeout=7)
        self.assertEqual(result, b"PEMDATA")

    def test_fetch_dpu_cert_never_falls_back_to_plaintext(self):
        """A TLS fetch failure raises -- there is no plaintext code path."""
        with patch('gnoi_shutdown_daemon.ssl.get_server_certificate', side_effect=OSError("connection refused")):
            with self.assertRaises(OSError):
                gnoi_shutdown_daemon._fetch_dpu_cert_pem("10.0.0.5", "8080", timeout=1)

    def test_build_dpu_endpoint_passes_fetched_cert_as_root_certificates(self):
        """The exact leaf cert fetched from the DPU is what gets pinned as
        root_certificates -- proves no separate/assumed trust anchor is
        used, and matches the maintainer-approved pattern exactly (no
        target-name override)."""
        pem = b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
        with patch('gnoi_shutdown_daemon._fetch_dpu_cert_pem', return_value=pem) as mock_fetch, \
             patch('gnoi_shutdown_daemon.grpc.ssl_channel_credentials') as mock_creds:
            mock_creds.return_value = MagicMock(name="creds")
            endpoint = gnoi_shutdown_daemon._build_dpu_endpoint("10.0.0.5", "8080", timeout=5)

        mock_fetch.assert_called_once_with("10.0.0.5", "8080", 5)
        mock_creds.assert_called_once_with(root_certificates=pem)
        self.assertEqual(endpoint.port, "8080")
        self.assertEqual(endpoint.credentials, mock_creds.return_value)

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

    def _poll(self, response=None, error=None, monotonic_values=None):
        """Shared helper for the RebootStatus acceptance-matrix tests below."""
        endpoint = _endpoint("8080")
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        behavior = error if error is not None else response
        cls, client = _mock_gnoi_client(system_reboot_status=behavior)
        with patch('gnoi_shutdown_daemon.GnoiClient', cls), \
             patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60), \
             patch('gnoi_shutdown_daemon.time.sleep'), \
             patch('gnoi_shutdown_daemon.time.monotonic', side_effect=monotonic_values or [0, 1, 61]):
            result = handler._poll_reboot_status("DPU0", "10.0.0.1", endpoint)
        return result, client, cls, endpoint

    def test_poll_reboot_status_failure(self):
        """Test _poll_reboot_status with an RPC failure -- a probe/RPC
        failure is a bounded retry, not success, and every attempt still
        carries TLS credentials (no plaintext fallback)."""
        result, client, cls, endpoint = self._poll(error=_FakeRpcError())
        self.assertFalse(result)
        # RebootStatus uses a real system_pb2.RebootStatusRequest().
        request_arg = client.system.RebootStatus.call_args.args[0]
        self.assertEqual(request_arg, system_pb2.RebootStatusRequest())
        self.assertEqual(cls.call_args.kwargs.get('credentials'), endpoint.credentials)

    def test_poll_reboot_status_active_false_status_success(self):
        """active=False + STATUS_SUCCESS => success."""
        resp = system_pb2.RebootStatusResponse()
        resp.active = False
        resp.status.status = system_pb2.RebootStatus.Status.STATUS_SUCCESS
        result, _, _, _ = self._poll(response=resp, monotonic_values=[0, 1])
        self.assertTrue(result)

    def test_poll_reboot_status_active_false_no_status_field_legacy_success(self):
        """active=False + status field absent => legacy success."""
        resp = system_pb2.RebootStatusResponse()
        resp.active = False  # status left unset entirely
        result, _, _, _ = self._poll(response=resp, monotonic_values=[0, 1])
        self.assertTrue(result)
        self.assertFalse(resp.HasField("status"))

    def test_poll_reboot_status_active_true_continues_polling(self):
        """active=True => not success (bounded retry until timeout)."""
        resp = system_pb2.RebootStatusResponse()
        resp.active = True
        result, client, _, _ = self._poll(response=resp, monotonic_values=[0, 1, 61])
        self.assertFalse(result)
        self.assertGreaterEqual(client.system.RebootStatus.call_count, 1)

    def test_poll_reboot_status_retriable_failure_not_success(self):
        resp = system_pb2.RebootStatusResponse()
        resp.active = False
        resp.status.status = system_pb2.RebootStatus.Status.STATUS_RETRIABLE_FAILURE
        result, _, _, _ = self._poll(response=resp)
        self.assertFalse(result)

    def test_poll_reboot_status_failure_status_not_success(self):
        resp = system_pb2.RebootStatusResponse()
        resp.active = False
        resp.status.status = system_pb2.RebootStatus.Status.STATUS_FAILURE
        result, _, _, _ = self._poll(response=resp)
        self.assertFalse(result)

    def test_poll_reboot_status_unknown_not_success(self):
        resp = system_pb2.RebootStatusResponse()
        resp.active = False
        resp.status.status = system_pb2.RebootStatus.Status.STATUS_UNKNOWN
        result, _, _, _ = self._poll(response=resp)
        self.assertFalse(result)

    def test_poll_reboot_status_rpc_error_logged_only_once(self):
        """Repeated identical RPC errors (e.g. the DPU going unreachable
        right after HALT) must not spam a warning on every 1-second retry."""
        endpoint = _endpoint("8080")
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        cls, client = _mock_gnoi_client(system_reboot_status=_FakeRpcError())
        with patch('gnoi_shutdown_daemon.GnoiClient', cls), \
             patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60), \
             patch('gnoi_shutdown_daemon.time.sleep'), \
             patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 1, 2, 3, 61]), \
             patch('gnoi_shutdown_daemon.logger') as mock_logger:
            handler._poll_reboot_status("DPU0", "10.0.0.1", endpoint)
        self.assertEqual(mock_logger.log_warning.call_count, 1)

    def test_poll_reboot_status_rpc_timeout_capped_to_remaining_deadline(self):
        """If only 3 seconds remain in the overall halt deadline, the
        RebootStatus RPC must be given timeout=3, not the full
        STATUS_RPC_TIMEOUT_SEC(10) -- otherwise the last RPC could run past
        dpu_halt_services_timeout."""
        endpoint = _endpoint("8080")
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        resp = system_pb2.RebootStatusResponse()
        resp.active = False
        resp.status.status = system_pb2.RebootStatus.Status.STATUS_SUCCESS
        cls, client = _mock_gnoi_client(system_reboot_status=resp)
        with patch('gnoi_shutdown_daemon.GnoiClient', cls), \
             patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=10), \
             patch('gnoi_shutdown_daemon.time.sleep'), \
             patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 7]):
            result = handler._poll_reboot_status("DPU0", "10.0.0.1", endpoint)
        self.assertTrue(result)
        self.assertEqual(client.system.RebootStatus.call_args.kwargs.get('timeout'), 3)
        self.assertLess(3, gnoi_shutdown_daemon.STATUS_RPC_TIMEOUT_SEC)

    def test_poll_reboot_status_sleep_capped_to_remaining_deadline(self):
        """The inter-poll sleep must never extend past the overall halt
        deadline: with only 0.5s left, sleep(min(STATUS_POLL_INTERVAL_SEC,
        remaining)) must be 0.5, not the full STATUS_POLL_INTERVAL_SEC."""
        endpoint = _endpoint("8080")
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        resp = system_pb2.RebootStatusResponse()
        resp.active = True  # not success -> falls through to the sleep
        cls, client = _mock_gnoi_client(system_reboot_status=resp)
        with patch('gnoi_shutdown_daemon.GnoiClient', cls), \
             patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=10), \
             patch('gnoi_shutdown_daemon.time.sleep') as mock_sleep, \
             patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 8, 9.5, 10.5]):
            result = handler._poll_reboot_status("DPU0", "10.0.0.1", endpoint)
        self.assertFalse(result)
        mock_sleep.assert_called_once_with(0.5)
        self.assertLess(0.5, gnoi_shutdown_daemon.STATUS_POLL_INTERVAL_SEC)

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

    def test_get_dpu_gnmi_ports_exception(self):
        """Test get_dpu_gnmi_ports when exception occurs."""
        mock_config = MagicMock()
        mock_config.hget.side_effect = AttributeError("Database error")

        ports = gnoi_shutdown_daemon.get_dpu_gnmi_ports(mock_config, "DPU1")
        self.assertEqual(ports, ["8080", "50052"])

    def test_send_reboot_command_success(self):
        """Test successful _send_reboot_command."""
        endpoint = _endpoint("50052")
        cls, client = _mock_gnoi_client(system_reboot=system_pb2.RebootResponse())
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon.GnoiClient', cls):
            result = handler._send_reboot_command("DPU0", "10.0.0.1", endpoint)

        self.assertTrue(result)
        client.system.Reboot.assert_called_once()
        request_arg = client.system.Reboot.call_args.args[0]
        self.assertEqual(request_arg.method, system_pb2.HALT)
        # GnoiClient is constructed against the selected port with TLS
        # credentials -- never a bare/plaintext direct-DPU call.
        self.assertEqual(cls.call_args.args[0], "10.0.0.1:50052")
        self.assertEqual(cls.call_args.kwargs.get('credentials'), endpoint.credentials)

    def test_find_working_port_falls_back_to_native_port(self):
        """Test that a failed configured port probe falls back to native gNMI."""
        def gnoi_client_side_effect(target, credentials=None):
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False
            if target == "10.0.0.1:8080":
                client.system.Time.side_effect = _FakeRpcError()
            else:
                client.system.Time.return_value = system_pb2.TimeResponse(time=1)
            return client

        with patch('gnoi_shutdown_daemon._build_dpu_endpoint',
                   side_effect=lambda dpu_ip, port, timeout: gnoi_shutdown_daemon.DpuEndpoint(port=port, credentials=MagicMock())), \
             patch('gnoi_shutdown_daemon.GnoiClient', side_effect=gnoi_client_side_effect) as mock_gnoi_client:
            handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
            result = handler._find_working_port("DPU0", "10.0.0.1", ["8080", "50052"])

        self.assertEqual(result.port, "50052")
        targets = [c.args[0] for c in mock_gnoi_client.call_args_list]
        self.assertEqual(targets, ["10.0.0.1:8080", "10.0.0.1:50052"])
        for c in mock_gnoi_client.call_args_list:
            self.assertIsNotNone(c.kwargs.get('credentials'))
        self.assertTrue(all("Reboot" not in str(c) for c in mock_gnoi_client.method_calls))

    def test_find_working_port_configured_port_tried_first(self):
        """The configured port (first in the list) is probed first and
        selected when it works, without probing the rest."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        endpoint = _endpoint("12345")
        with patch.object(handler, '_probe_port', return_value=endpoint) as mock_probe:
            result = handler._find_working_port("DPU0", "10.0.0.1", ["12345", "8080", "50052"])
        self.assertIs(result, endpoint)
        mock_probe.assert_called_once_with("DPU0", "10.0.0.1", "12345")

    def test_find_working_port_falls_back_to_50052(self):
        """Configured port and 8080 both fail; 50052 is tried and succeeds."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        endpoint_50052 = _endpoint("50052")
        with patch.object(handler, '_probe_port', side_effect=[None, None, endpoint_50052]) as mock_probe:
            result = handler._find_working_port("DPU0", "10.0.0.1", ["12345", "8080", "50052"])
        self.assertIs(result, endpoint_50052)
        self.assertEqual(mock_probe.call_count, 3)

    def test_find_working_port_all_fail_returns_none(self):
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch.object(handler, '_probe_port', return_value=None) as mock_probe:
            result = handler._find_working_port("DPU0", "10.0.0.1", ["8080", "50052"])
        self.assertIsNone(result)
        self.assertEqual(mock_probe.call_count, 2)

    def test_probe_port_malformed_configured_port_falls_back_cleanly(self):
        """A malformed configured gnmi_port (e.g. non-numeric) must fail
        that one candidate and let probing continue to 8080/50052, not
        raise and abort the whole transition. _fetch_dpu_cert_pem passing
        the port through unchanged is covered separately
        (test_fetch_dpu_cert_pem_uses_ssl_get_server_certificate); this
        test only needs to prove _probe_port's own handling of whatever
        OSError a bad port produces -- it doesn't depend on the host's
        resolver/services file to get one."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        with patch('gnoi_shutdown_daemon._build_dpu_endpoint',
                   side_effect=OSError("Servname not supported for ai_socktype")), \
             patch('gnoi_shutdown_daemon.GnoiClient') as mock_gnoi_client_cls:
            try:
                result = handler._probe_port("DPU0", "10.0.0.1", "not-a-port")
            except Exception as e:
                self.fail(f"_probe_port raised on a malformed port instead of failing closed: {e!r}")
        self.assertIsNone(result)
        mock_gnoi_client_cls.assert_not_called()

    def test_find_working_port_malformed_configured_port_then_8080(self):
        """End-to-end: malformed configured port -> candidate fails ->
        8080 is tried next -> probing does not throw."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        endpoint_8080 = _endpoint("8080")
        with patch.object(handler, '_probe_port', side_effect=[None, endpoint_8080]) as mock_probe:
            result = handler._find_working_port("DPU0", "10.0.0.1", ["not-a-port", "8080", "50052"])
        self.assertIs(result, endpoint_8080)
        self.assertEqual(mock_probe.call_args_list, [
            call("DPU0", "10.0.0.1", "not-a-port"),
            call("DPU0", "10.0.0.1", "8080"),
        ])

    def test_probe_port_shares_one_timeout_budget_across_cert_fetch_and_time_rpc(self):
        """Certificate fetch and System.Time must share ONE
        PORT_PROBE_TIMEOUT_SEC budget for a candidate port, not two
        independent timeouts -- otherwise probing configured+8080+50052
        could add up to ~2x PORT_PROBE_TIMEOUT_SEC per port before Reboot
        is even attempted."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        captured = {}

        def gnoi_client_side_effect(target, credentials=None):
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False

            def record_time(request, timeout):
                captured['timeout'] = timeout
                return system_pb2.TimeResponse(time=1)

            client.system.Time.side_effect = record_time
            return client

        # Simulate time.monotonic() advancing by 4 seconds while the
        # (mocked) certificate fetch runs, before System.Time is called.
        with patch('gnoi_shutdown_daemon.time.monotonic', side_effect=[0, 4]), \
             patch('gnoi_shutdown_daemon._build_dpu_endpoint',
                   return_value=gnoi_shutdown_daemon.DpuEndpoint(port="8080", credentials=MagicMock())), \
             patch('gnoi_shutdown_daemon.GnoiClient', side_effect=gnoi_client_side_effect):
            handler._probe_port("DPU0", "10.0.0.1", "8080")

        self.assertEqual(captured['timeout'], gnoi_shutdown_daemon.PORT_PROBE_TIMEOUT_SEC - 4)

    def test_handle_transition_clears_halt_flag_when_all_ports_fail(self):
        """Test cleanup when no configured or common gNMI port responds."""
        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), MagicMock())
        handler._should_skip_gnoi_shutdown = MagicMock(return_value=False)
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        handler._find_working_port = MagicMock(return_value=None)
        handler._clear_halt_flag = MagicMock(return_value=True)

        with patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1"), \
                patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports', return_value=["8080", "50052"]):
            result = handler._handle_transition("DPU0")

        self.assertFalse(result)
        handler._find_working_port.assert_called_once_with(
            "DPU0", "10.0.0.1", ["8080", "50052"])
        handler._clear_halt_flag.assert_called_once_with("DPU0")

    @patch('gnoi_shutdown_daemon._get_halt_timeout', return_value=60)
    @patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1")
    @patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports', side_effect=Exception("Port lookup failed"))
    def test_handle_transition_config_exception(self, mock_get_port, mock_get_ip, mock_get_halt_timeout):
        """Test handle_transition when configuration lookup raises exception."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module for clear operation
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)

        # Mock _wait_for_gnoi_halt_in_progress to return immediately to prevent hanging
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)

        result = handler._handle_transition("DPU0")

        self.assertFalse(result)
        # Verify that clear_module_gnoi_halt_in_progress was called
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_should_skip_gnoi_shutdown_offline(self):
        """Test _should_skip_gnoi_shutdown returns True for Offline DPU."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertTrue(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_powered_down(self):
        """Test _should_skip_gnoi_shutdown returns True for PoweredDown DPU."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_POWERED_DOWN
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertTrue(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_online(self):
        """Test _should_skip_gnoi_shutdown returns False for Online DPU."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_ONLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertFalse(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_fault(self):
        """Test _should_skip_gnoi_shutdown returns False for Fault DPU."""
        mock_chassis = MagicMock()
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_FAULT
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertFalse(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_bad_index(self):
        """Test _should_skip_gnoi_shutdown returns None when module index is negative."""
        mock_chassis = MagicMock()
        mock_chassis.get_module_index.return_value = -1

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertIsNone(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_should_skip_gnoi_shutdown_no_module(self):
        """Test _should_skip_gnoi_shutdown returns None when module is None."""
        mock_chassis = MagicMock()
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = None

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(MagicMock(), MagicMock(), mock_chassis)
        self.assertIsNone(handler._should_skip_gnoi_shutdown("DPU0"))

    def test_handle_transition_dpu_already_offline(self):
        """Test that gNOI shutdown is skipped when DPU is already offline."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module with Offline oper_status
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        result = handler._handle_transition("DPU0")

        # Should return True (success) without attempting gNOI reboot
        self.assertTrue(result)
        mock_module.get_oper_status.assert_called_once()
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_handle_transition_dpu_powered_down(self):
        """Test that gNOI shutdown is skipped when DPU is in PoweredDown state."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module with PoweredDown oper_status
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_POWERED_DOWN
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        result = handler._handle_transition("DPU0")

        # Should return True (success) without attempting gNOI reboot
        self.assertTrue(result)
        mock_module.get_oper_status.assert_called_once()
        mock_module.clear_module_gnoi_halt_in_progress.assert_called_once()

    def test_handle_transition_dpu_fault_proceeds(self):
        """Test that gNOI shutdown proceeds when DPU is in Fault state."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module with Fault oper_status — should NOT skip
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_FAULT
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)

        # Mock remaining methods to prevent actual gNOI calls
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        handler._find_working_port = MagicMock(return_value="8080")
        handler._send_reboot_command = MagicMock(return_value=True)
        handler._poll_reboot_status = MagicMock(return_value=True)
        handler._clear_halt_flag = MagicMock(return_value=True)

        with patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1"), \
             patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports', return_value=["8080", "50052"]):
            result = handler._handle_transition("DPU0")

        # Should proceed with shutdown for Fault state
        self.assertTrue(result)
        handler._wait_for_gnoi_halt_in_progress.assert_called_once()
        handler._send_reboot_command.assert_called_once()

    def test_handle_transition_dpu_offline_clear_halt_failure(self):
        """Test that _clear_halt_flag failure is propagated when DPU is offline."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module with Offline oper_status
        mock_module = MagicMock()
        mock_module.get_oper_status.return_value = ModuleBase.MODULE_STATUS_OFFLINE
        mock_chassis.get_module_index.return_value = 0
        mock_chassis.get_module.return_value = mock_module

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)
        # Make _clear_halt_flag fail
        handler._clear_halt_flag = MagicMock(return_value=False)

        result = handler._handle_transition("DPU0")

        # Should return False since _clear_halt_flag failed
        self.assertFalse(result)
        handler._clear_halt_flag.assert_called_once_with("DPU0")

    def test_handle_transition_oper_status_check_exception(self):
        """Test that gNOI shutdown proceeds when oper_status check raises exception."""
        mock_db = MagicMock()
        mock_config_db = MagicMock()
        mock_chassis = MagicMock()

        # Mock module to raise exception on get_module_index
        mock_chassis.get_module_index.side_effect = Exception("Platform error")

        handler = gnoi_shutdown_daemon.GnoiRebootHandler(mock_db, mock_config_db, mock_chassis)

        # Mock remaining methods to prevent actual gNOI calls
        handler._wait_for_gnoi_halt_in_progress = MagicMock(return_value=True)
        handler._find_working_port = MagicMock(return_value="8080")
        handler._send_reboot_command = MagicMock(return_value=True)
        handler._poll_reboot_status = MagicMock(return_value=True)
        handler._clear_halt_flag = MagicMock(return_value=True)

        with patch('gnoi_shutdown_daemon.get_dpu_ip', return_value="10.0.0.1"), \
             patch('gnoi_shutdown_daemon.get_dpu_gnmi_ports', return_value=["8080", "50052"]):
            result = handler._handle_transition("DPU0")

        # Should proceed with shutdown despite oper_status check failure
        self.assertTrue(result)
        handler._wait_for_gnoi_halt_in_progress.assert_called_once()
        handler._send_reboot_command.assert_called_once()

    @patch('gnoi_shutdown_daemon.daemon_base.db_connect')
    @patch('gnoi_shutdown_daemon.GnoiRebootHandler')
    @patch('gnoi_shutdown_daemon.swsscommon.ConfigDBConnector')
    @patch('threading.Thread')
    def test_handle_and_cleanup_per_thread_connections(self, mock_thread, mock_config_db_connector_class, mock_gnoi_reboot_handler, mock_db_connect):
        """Test that handle_and_cleanup opens per-thread DB connections and passes them to _handle_transition."""
        mock_state_db = MagicMock()
        mock_config_db = MagicMock()
        mock_thread_config_db = MagicMock()
        mock_thread_state_db = MagicMock()

        # main() calls db_connect("STATE_DB") then ("CONFIG_DB");
        # handle_and_cleanup then calls ("CONFIG_DB") and ("STATE_DB") for its own thread.
        mock_db_connect.side_effect = [mock_state_db, mock_config_db,
                                       mock_thread_config_db, mock_thread_state_db]
        mock_config_db.hget.return_value = "down"

        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = [mock_message, KeyboardInterrupt]
        mock_redis_client = MagicMock()
        mock_redis_client.pubsub.return_value = mock_pubsub
        mock_config_db_connector = MagicMock()
        mock_config_db_connector.db_name = "CONFIG_DB"
        mock_config_db_connector.get_redis_client.return_value = mock_redis_client
        mock_config_db_connector_class.return_value = mock_config_db_connector

        mock_handler_instance = MagicMock()
        mock_gnoi_reboot_handler.return_value = mock_handler_instance

        mock_platform_submodule = MagicMock()
        mock_sonic_platform = MagicMock()
        mock_sonic_platform.platform = mock_platform_submodule

        with patch.dict('sys.modules', {
            'sonic_platform': mock_sonic_platform,
            'sonic_platform.platform': mock_platform_submodule
        }):
            with self.assertRaises(KeyboardInterrupt):
                gnoi_shutdown_daemon.main()

        # Capture the thread target and run it synchronously to exercise handle_and_cleanup
        thread_call_kwargs = mock_thread.call_args.kwargs
        target_fn = thread_call_kwargs['target']
        target_args = thread_call_kwargs['args']
        target_fn(*target_args)

        # Verify per-thread connections were opened
        mock_db_connect.assert_any_call("CONFIG_DB")
        mock_db_connect.assert_any_call("STATE_DB")

        # Verify _handle_transition received the per-thread connections, not the shared ones
        mock_handler_instance._handle_transition.assert_called_once_with(
            "DPU0",
            config_db=mock_thread_config_db,
            state_db=mock_thread_state_db,
        )


if __name__ == '__main__':
    unittest.main()
