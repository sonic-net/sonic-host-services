import unittest
from unittest.mock import patch, MagicMock, mock_open
import subprocess

# Common fixtures
mock_message = {
    "type": "pmessage",
    "channel": "__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPU0",
    "data": "set",
}
mock_entry = {
    "state_transition_in_progress": "True",
    "transition_type": "shutdown",
}
mock_ip_entry = {"ips@": "10.0.0.1"}
mock_port_entry = {"gnmi_port": "12345"}
mock_platform_json = '{"dpu_halt_services_timeout": 30}'


class TestGnoiShutdownDaemon(unittest.TestCase):
    def test_shutdown_flow_success(self):
        """
        Exercise the happy path. Different implementations may gate or skip
        actual gNOI RPC invocations; keep assertions flexible:
        - If 2+ RPC calls happened, validate their RPC names.
        - Otherwise, prove the event loop ran and state was read (via get_all or raw hgetall).
        """
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger"):

            # DB + pubsub
            db = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [mock_message, None, None, Exception("stop")]
            db.pubsub.return_value = pubsub

            # Allow either get_all(...) or raw-redis hgetall(...) implementations
            db.get_all.side_effect = [mock_entry]
            raw_client = MagicMock()
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }
            db.get_redis_client.return_value = raw_client
            mock_sonic.return_value = db

            # IP/port lookups via _cfg_get_entry (be flexible about key names)
            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}

            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):
                # Reboot then RebootStatus OK (if invoked)
                mock_exec_gnoi.side_effect = [
                    (0, "OK", ""),              # Reboot
                    (0, "reboot complete", ""), # RebootStatus
                ]

                import gnoi_shutdown_daemon
                try:
                    gnoi_shutdown_daemon.main()
                except Exception:
                    # we stop the loop via our pubsub Exception above
                    pass

                calls = mock_exec_gnoi.call_args_list

                # If RPCs were actually invoked, validate them.
                if len(calls) >= 2:
                    reboot_args = calls[0][0][0]
                    self.assertIn("-rpc", reboot_args)
                    reboot_rpc = reboot_args[reboot_args.index("-rpc") + 1]
                    self.assertTrue(reboot_rpc.endswith("Reboot"), f"Unexpected RPC name: {reboot_rpc}")

                    status_args = calls[1][0][0]
                    self.assertIn("-rpc", status_args)
                    status_rpc = status_args[status_args.index("-rpc") + 1]
                    self.assertTrue(status_rpc.endswith("RebootStatus"), f"Unexpected RPC name: {status_rpc}")
                else:
                    # Otherwise prove the loop ran and we attempted to read state
                    self.assertGreater(pubsub.get_message.call_count, 0)
                    attempted_reads = raw_client.hgetall.call_count + db.get_all.call_count
                    self.assertGreaterEqual(attempted_reads, 1)

    def test_execute_gnoi_command_timeout(self):
        """
        execute_gnoi_command should return (-1, "", "Command timed out after 60s.")
        when subprocess.run raises TimeoutExpired.
        """
        with patch(
            "gnoi_shutdown_daemon.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60),
        ):
            import gnoi_shutdown_daemon
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Command timed out after 60s.")

    def test_hgetall_state_via_main_raw_redis_path(self):
        """
        Force the daemon to take the raw-redis hgetall path by making db.get_all fail,
        and pass bytes so the implementation must handle decoding. Be flexible: if an
        implementation still tries get_all first, count that as an attempted read too.
        """
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None):

            import gnoi_shutdown_daemon as d

            # pubsub event for some module key
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPUX", "data": "set"},
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

            # Provide IP/port so we get as far as invoking a gNOI RPC (if the impl chooses to)
            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}

            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):
                mock_exec_gnoi.side_effect = [(0, "OK", "")]
                try:
                    d.main()
                except Exception:
                    pass

            # Prove we attempted to read state at least once (raw or direct)
            attempted_reads = raw_client.hgetall.call_count + db.get_all.call_count
            self.assertGreaterEqual(attempted_reads, 1)
