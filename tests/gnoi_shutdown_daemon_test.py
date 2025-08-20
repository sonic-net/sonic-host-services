import unittest
from unittest.mock import patch, MagicMock, mock_open
import subprocess

# Simulated message and DB content
mock_message = {
    'type': 'pmessage',
    'channel': '__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|DPU0',
    'data': 'set'
}

mock_entry = {
    'state_transition_in_progress': 'True',
    'transition_type': 'shutdown'
}

mock_platform_json = '{"dpu_halt_services_timeout": 30}'

@patch("gnoi_shutdown_daemon.SonicV2Connector")
@patch("gnoi_shutdown_daemon.execute_gnoi_command")
@patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json)
@patch("gnoi_shutdown_daemon.time.sleep", return_value=None)
class TestGnoiShutdownDaemon(unittest.TestCase):

    def test_shutdown_flow_success(self, mock_sleep, mock_open_fn, mock_exec_gnoi, mock_sonic):
        db_instance = MagicMock()
        pubsub = MagicMock()
        pubsub.get_message.side_effect = [mock_message, None, None, None]
        db_instance.pubsub.return_value = pubsub
        db_instance.get_all.side_effect = [mock_entry]  # STATE_DB HGETALL
        mock_sonic.return_value = db_instance

        # gNOI client calls: Reboot then RebootStatus
        mock_exec_gnoi.side_effect = [
            (0, "OK", ""),
            (0, "reboot complete", ""),
        ]

        import gnoi_shutdown_daemon
        gnoi_shutdown_daemon.logger = MagicMock()

        # Provide CONFIG_DB rows via _cfg_get_entry (daemon’s current path)
        def _fake_cfg(table, key):
            if table == "DHCP_SERVER_IPV4_PORT" and key == "bridge-midplane|dpu0":
                return {"ips@": "10.0.0.1"}
            if table == "DPU_PORT" and key in ("DPU0", "dpu0"):
                return {"gnmi_port": "12345"}
            if table == "DEVICE_METADATA" and key == "localhost":
                return {"platform": "cisco-8101"}
            return {}

        with patch.object(gnoi_shutdown_daemon, "_cfg_get_entry", side_effect=_fake_cfg):
            # Run one iteration of the main loop (guarded)
            with patch("builtins.__import__"):
                try:
                    gnoi_shutdown_daemon.main()
                except Exception:
                    pass

        # Validate gNOI invocations
        calls = mock_exec_gnoi.call_args_list
        assert len(calls) >= 2, "Expected at least 2 gNOI calls"

        # Reboot
        cmd_args = calls[0][0][0]
        assert "-rpc" in cmd_args
        i = cmd_args.index("-rpc")
        assert cmd_args[i + 1] == "Reboot"

        # RebootStatus
        status_args = calls[1][0][0]
        assert "-rpc" in status_args
        i = status_args.index("-rpc")
        assert status_args[i + 1] == "RebootStatus"


# Keep this test OUTSIDE the class so it doesn’t receive the class-level patches
@patch("gnoi_shutdown_daemon.subprocess.run",
       side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60))
def test_execute_gnoi_command_timeout(mock_run):
    import gnoi_shutdown_daemon
    rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
    assert rc == -1
    assert stdout == ""
    assert stderr == "Command timed out."
