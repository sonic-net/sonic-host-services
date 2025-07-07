import unittest
from unittest.mock import patch, MagicMock, mock_open
import json

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

mock_ip_entry = {"ips@": "10.0.0.1"}
mock_port_entry = {"gnmi_port": "12345"}
mock_platform_entry = {"platform": "cisco-8101"}
mock_platform_json = '{"dpu_halt_services_timeout": 30}'

@patch("gnoi_shutdown_daemon.SonicV2Connector")
@patch("gnoi_shutdown_daemon.execute_gnoi_command")
@patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json)
@patch("gnoi_shutdown_daemon.time.sleep", return_value=None)
class TestGnoiShutdownDaemon(unittest.TestCase):

    def test_shutdown_flow_success(self, mock_sleep, mock_open_fn, mock_exec_gnoi, mock_sonic):
        db_instance = MagicMock()
        pubsub = MagicMock()
        pubsub.get_message.side_effect = [
            mock_message, None, None, None
        ]
        db_instance.pubsub.return_value = pubsub
        db_instance.get_all.side_effect = [mock_entry]
        db_instance.get_entry.side_effect = [
            mock_ip_entry,        # for get_dpu_ip
            mock_port_entry,      # for get_gnmi_port
            mock_platform_entry   # for platform
        ]

        mock_exec_gnoi.side_effect = [
            (0, "OK", ""),                   # gnoi_client Reboot
            (0, "reboot complete", ""),      # gnoi_client RebootStatus
        ]

        mock_sonic.return_value = db_instance

        import gnoi_shutdown_daemon
        gnoi_shutdown_daemon.logger = MagicMock()

        # Run one iteration of the main loop
        with patch("builtins.__import__"):
            try:
                gnoi_shutdown_daemon.main()
            except Exception:
                pass  # Prevent infinite loop

        # Validate gNOI command sequence
        calls = mock_exec_gnoi.call_args_list
        assert "Reboot" in calls[0][0][0][-2]
        assert "RebootStatus" in calls[1][0][0][-2]

        # Check STATE_DB update
        db_instance.set.assert_called_with(
            "STATE_DB",
            "CHASSIS_MODULE_INFO_TABLE|DPU0",
            {"state_transition_in_progress": "False", "transition_type": "none"},
        )
