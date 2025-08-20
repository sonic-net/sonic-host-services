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

# --- Tiny fake swsscommon to cover Table fallback in _hset_state ---
class _FakeFieldValuePairs(list):
    pass

class _FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self._sets = []

    def set(self, obj, fvp):
        # store for optional inspection
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
             patch("gnoi_shutdown_daemon.logger") as mock_logger:

            # DB + pubsub
            db_instance = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [mock_message, None, None, Exception("stop")]
            db_instance.pubsub.return_value = pubsub
            db_instance.get_all.side_effect = [mock_entry]
            mock_sonic.return_value = db_instance

            # IP/port lookups via _cfg_get_entry
            def _cfg_get_entry_side(table, key):
                if table == "DHCP_SERVER_IPV4_PORT" and key.startswith("bridge-midplane|"):
                    return mock_ip_entry
                if table == "DPU_PORT":
                    return mock_port_entry
                return {}
            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):

                # Reboot then RebootStatus OK
                mock_exec_gnoi.side_effect = [
                    (0, "OK", ""),                   # Reboot
                    (0, "reboot complete", ""),      # RebootStatus
                ]

                import gnoi_shutdown_daemon
                # Run one iteration (we stop via the Exception above)
                try:
                    gnoi_shutdown_daemon.main()
                except Exception:
                    pass

                calls = mock_exec_gnoi.call_args_list
                self.assertGreaterEqual(len(calls), 2, "Expected at least 2 gNOI calls")

                # Validate Reboot call
                reboot_args = calls[0][0][0]
                self.assertIn("-rpc", reboot_args)
                self.assertEqual(reboot_args[reboot_args.index("-rpc") + 1], "Reboot")

                # Validate RebootStatus call
                status_args = calls[1][0][0]
                self.assertIn("-rpc", status_args)
                self.assertEqual(status_args[status_args.index("-rpc") + 1], "RebootStatus")

    def test_execute_gnoi_command_timeout(self):
        with patch("gnoi_shutdown_daemon.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60)):
            import gnoi_shutdown_daemon
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Command timed out after 60s.")

    def test_hgetall_state_raw_redis_path(self):
        # Force _hgetall_state to use raw redis client and decode bytes
        import gnoi_shutdown_daemon as d

        raw_client = MagicMock()
        raw_client.hgetall.return_value = {b"a": b"1", b"b": b"2"}

        db = MagicMock()
        db.get_all.side_effect = Exception("no direct get_all")
        db.get_redis_client.return_value = raw_client

        out = d._hgetall_state(db, "CHASSIS_MODULE_INFO_TABLE|DPUX")
        self.assertEqual(out, {"a": "1", "b": "2"})

    def test_hset_state_table_fallback(self):
        # Drive _hset_state through hmset AttributeError and hset AttributeError to Table fallback
        with patch.dict("sys.modules", {
            "swsscommon": types.SimpleNamespace(swsscommon=_fake_swsscommon_mod),
            "swsscommon.swsscommon": _fake_swsscommon_mod,
        }):
            import gnoi_shutdown_daemon as d

            db = MagicMock()
            db.hmset.side_effect = AttributeError("no hmset")
            db.hset.side_effect = AttributeError("no hset")

            # Should not raise; should call Table(...).set(...)
            d._hset_state(db, "CHASSIS_MODULE_INFO_TABLE|DPU9", {"k1": "v1", "k2": 2})

    def test_get_pubsub_raw_path_and_no_ip_branch_in_main(self):
        # Cover _get_pubsub raw path and main() error branch when DPU IP is missing
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:

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
            db.get_all.return_value = {"state_transition_in_progress": "True", "transition_type": "shutdown"}
            mock_sonic.return_value = db

            # No IP returned -> error branch
            with patch("gnoi_shutdown_daemon._cfg_get_entry", return_value={}):
                try:
                    d.main()
                except Exception:
                    pass

            mock_logger.log_error.assert_any_call("Error getting DPU IP or port for DPU0: DPU IP not found")
