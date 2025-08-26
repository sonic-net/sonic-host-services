import unittest
from unittest.mock import patch, MagicMock, mock_open
import types
import subprocess

mock_message = {
    'type': 'pmessage',
    'channel': '__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPU0',
    'data': 'set'
}

mock_entry = {
    'state_transition_in_progress': 'True',
    'transition_type': 'shutdown'
}

mock_ip_entry = {"ips@": "10.0.0.1"}
mock_port_entry = {"gnmi_port": "12345"}
mock_platform_json = '{"dpu_halt_services_timeout": 30}'  # read by open() in some paths

# fake swsscommon to cover Table fallback IF the module exposes it
class _FakeFieldValuePairs(list):
    pass

class _FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self._sets = []

    def set(self, obj, fvp):
        # record for optional inspection
        self._sets.append((obj, list(fvp)))

_fake_swsscommon_mod = types.SimpleNamespace(
    FieldValuePairs=_FakeFieldValuePairs,
    Table=_FakeTable,
)

class TestGnoiShutdownDaemon(unittest.TestCase):
    def test_shutdown_flow_success(self):
        # Patch everything explicitly (no class-level decorators -> no arg-mismatch surprises)
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger"):

            # DB + pubsub
            db_instance = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [mock_message, None, None, Exception("stop")]
            db_instance.pubsub.return_value = pubsub

            # Allow either get_all(...) or raw-redis hgetall(...) implementations
            db_instance.get_all.side_effect = [mock_entry]
            raw_client = MagicMock()
            # bytes to ensure decoder-friendly behavior if the impl reads raw redis
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }
            db_instance.get_redis_client.return_value = raw_client
            mock_sonic.return_value = db_instance

            # IP/port lookups via _cfg_get_entry (be flexible about key names)
            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}

            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):

                # Reboot then RebootStatus OK
                mock_exec_gnoi.side_effect = [
                    (0, "OK", ""),                   # Reboot
                    (0, "reboot complete", ""),      # RebootStatus
                ]

                import gnoi_shutdown_daemon
                try:
                    gnoi_shutdown_daemon.main()
                except Exception:
                    # stop loop from our pubsub side-effect
                    pass

                calls = mock_exec_gnoi.call_args_list
                # In the happy path we really do want at least 2 RPCs.
                self.assertGreaterEqual(len(calls), 2, "Expected at least 2 gNOI calls")

                # Validate Reboot call
                reboot_args = calls[0][0][0]
                self.assertIn("-rpc", reboot_args)
                reboot_rpc = reboot_args[reboot_args.index("-rpc") + 1]
                self.assertTrue(reboot_rpc.endswith("Reboot"), f"Unexpected RPC name: {reboot_rpc}")

                # Validate RebootStatus call
                status_args = calls[1][0][0]
                self.assertIn("-rpc", status_args)
                status_rpc = status_args[status_args.index("-rpc") + 1]
                self.assertTrue(status_rpc.endswith("RebootStatus"), f"Unexpected RPC name: {status_rpc}")

    def test_execute_gnoi_command_timeout(self):
        with patch("gnoi_shutdown_daemon.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60)):
            import gnoi_shutdown_daemon
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Command timed out after 60s.")

    def test_hgetall_state_via_main_raw_redis_path(self):
        """
        Force the daemon to take the raw-redis hgetall path by making db.get_all fail,
        and pass bytes so the implementation must handle decoding.
        """
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None):

            import gnoi_shutdown_daemon as d

            # pubsub event for some module key
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage",
                 "channel": "__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPUX",
                 "data": "set"},
                Exception("stop"),
            ]

            # DB forcing fallback to raw redis path
            raw_client = MagicMock()
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }

            db = MagicMock()
            db.pubsub.return_value = pubsub
            db.get_all.side_effect = Exception("no direct get_all")
            db.get_redis_client.return_value = raw_client
            mock_sonic.return_value = db

            # Provide IP/port so we get as far as invoking a gNOI RPC
            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}
            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):

                # Make the first RPC succeed, then stop
                mock_exec_gnoi.side_effect = [(0, "OK", "")]
                try:
                    d.main()
                except Exception:
                    pass

            # Proved that raw redis path was taken and consumed
            self.assertGreaterEqual(raw_client.hgetall.call_count, 1)
            self.assertGreaterEqual(mock_exec_gnoi.call_count, 1)

    def test_get_pubsub_raw_path_and_no_ip_branch_in_main(self):
        # Cover _get_pubsub raw path and main() error branch when DPU IP is missing
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.logger"):

            import gnoi_shutdown_daemon as d

            # pubsub falls back to raw redis client
            raw_pub = MagicMock()
            raw_client = MagicMock()
            raw_client.pubsub.return_value = raw_pub

            db = MagicMock()
            db.pubsub.side_effect = AttributeError("no pubsub on this client")
            db.get_redis_client.return_value = raw_client

            # Event and entry to trigger shutdown path
            raw_pub.get_message.side_effect = [
                {"type": "pmessage",
                 "channel": "__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPU0",
                 "data": "set"},
                Exception("stop"),
            ]

            # Allow either get_all or raw-redis. Provide bytes for robustness.
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }
            db.get_all.return_value = {"state_transition_in_progress": "True", "transition_type": "shutdown"}
            mock_sonic.return_value = db

            # No IP returned -> error branch; do NOT expect any gNOI calls
            with patch("gnoi_shutdown_daemon._cfg_get_entry", return_value={}):
                try:
                    d.main()
                except Exception:
                    pass

            # We processed pubsub (raw path) and never invoked gNOI because IP was missing
            self.assertGreater(raw_pub.get_message.call_count, 0)
            mock_exec_gnoi.assert_not_called()
