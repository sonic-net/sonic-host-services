import os
import sys
import swsscommon
from unittest.mock import call, patch, MagicMock
from unittest import TestCase, mock
from tests.common.mock_configdb import MockConfigDb
from sonic_py_common.general import load_module_from_source
import threading
import sys
from queue import Queue


DBCONFIG_PATH = "/var/run/redis/sonic-db/database_config.json"


class TestCaclmgrd(TestCase):
    def setUp(self):
        swsscommon.swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, "caclmgrd")
        self.caclmgrd = load_module_from_source("caclmgrd", caclmgrd_path)

    def test_run_commands_pipe(self):
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        output = caclmgrd_daemon.run_commands_pipe(
            ["echo", "caclmgrd"], ["awk", "{print $1}"]
        )
        assert output == "caclmgrd"

        output = caclmgrd_daemon.run_commands_pipe(
            [sys.executable, "-c", "import sys; sys.exit(6)"],
            [sys.executable, "-c", "import sys; sys.exit(8)"],
        )
        assert output == ""

    def test_get_chain_list(self):
        expected_calls = [
            call(
                ["iptables", "-L", "-v", "-n"], ["grep", "Chain"], ["awk", "{print $2}"]
            )
        ]
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        with mock.patch(
            "caclmgrd.ControlPlaneAclManager.run_commands_pipe"
        ) as mock_run_commands_pipe:
            caclmgrd_daemon.get_chain_list([], [""])
            mock_run_commands_pipe.assert_has_calls(expected_calls)

    @patch("caclmgrd.ControlPlaneAclManager.update_control_plane_acls")
    def test_update_control_plane_acls_exception(self, mock_update):
        # Set the side effect to raise an exception
        mock_update.side_effect = Exception("Test exception")
        # Mock the necessary attributes and methods
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        manager.UPDATE_DELAY_SECS = 1
        manager.lock = {"": threading.Lock()}
        manager.num_changes = {"": 0}
        manager.update_thread = {"": None}
        exception_queue = Queue()
        manager.num_changes[""] = 1
        manager.check_and_update_control_plane_acls("", 0, exception_queue)
        self.assertFalse(exception_queue.empty())
        exc_info = exception_queue.get()
        self.assertEqual(exc_info[0], "")
        self.assertIn("Test exception", exc_info[1])

    @patch("caclmgrd.swsscommon")
    @patch("os.geteuid", return_value=0)
    @patch("os.kill")
    @patch("signal.SIGKILL", return_value=9)
    @patch("sys.exit")
    @patch("traceback.format_exception")
    def test_run(
        self,
        mock_format_exception,
        mock_exit,
        mock_sigkill,
        mock_kill,
        mock_geteuid,
        mock_swsscommon,
    ):
        # Setup
        mock_select_instance = MagicMock()
        mock_Select.return_value = mock_select_instance

        mock_getDbId.side_effect = lambda db_name: {"STATE_DB": 1, "CONFIG_DB": 2}.get(
            db_name, 0
        )

        mock_db_connector = MagicMock()
        mock_DBConnector.return_value = mock_db_connector

        exception_queue = Queue()

        # Mock self object and its attributes
        self_mock = MagicMock()
        self_mock.DualToR = True
        self_mock.bfdAllowed = False
        self_mock.VxlanAllowed = False
        self_mock.config_db_map = {"DEFAULT_NAMESPACE": MagicMock()}
        self_mock.lock = {"DEFAULT_NAMESPACE": threading.Lock()}
        self_mock.num_changes = {"DEFAULT_NAMESPACE": 0}
        self_mock.update_thread = {"DEFAULT_NAMESPACE": None}
        self_mock.MUX_CABLE_TABLE = "MUX_CABLE_TABLE"
        self_mock.BFD_SESSION_TABLE = "BFD_SESSION_TABLE"
        self_mock.VXLAN_TUNNEL_TABLE = "VXLAN_TUNNEL_TABLE"
        self_mock.ACL_TABLE = "ACL_TABLE"
        self_mock.ACL_TABLE_TYPE_CTRLPLANE = "CTRLPLANE"

        # Define expected behavior for self.log_error and self.log_info
        self_mock.log_error = MagicMock()
        self_mock.log_info = MagicMock()

        # Mock traceback formatting
        mock_format_exception.return_value = ["Traceback line 1", "Traceback line 2"]

        # Execute the `run` function
        try:
            TestRunFunction.run(self_mock)
        except SystemExit:
            pass  # Ignore the SystemExit exception

        # Assertions
        self_mock.log_info.assert_called_with("Starting up ...")
        mock_DBConnector.assert_any_call("STATE_DB", 0)
        mock_DBConnector.assert_any_call("CONFIG_DB", 0)
        self_mock.log_error.assert_not_called_with("Must be root to run this daemon")
        mock_initializeGlobalConfig.assert_called_once()
        mock_Select.assert_called_once()

        # Ensure signals were set correctly during exception
        if not exception_queue.empty():
            self_mock.log_error.assert_called()
            mock_kill.assert_called_once_with(os.getpid(), signal.SIGKILL)

        # Validate exit handling
        mock_exit.assert_not_called()

    @patch("caclmgrd.swsscommon")
    @patch("os.geteuid", return_value=0)
    @patch("os.kill")
    @patch("signal.SIGKILL", return_value=9)
    @patch("sys.exit")
    @patch("traceback.format_exception")
    def test_run(
        self,
        mock_format_exception,
        mock_exit,
        mock_sigkill,
        mock_kill,
        mock_geteuid,
        mock_swsscommon,
    ):
        mock_swsscommon.SonicDBConfig.getDbId.side_effect = lambda db_name: (
            0 if db_name == "STATE_DB" else 1
        )
        mock_swsscommon.Select.OBJECT = 1
        mock_swsscommon.Select.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.SubscriberStateTable.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getNamespace.return_value = (
            "default"
        )
        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getDbId.side_effect = (
            lambda: 0
        )

        # Creating an instance of ControlPlaneAclManager
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        # Setting necessary attributes
        manager.DualToR = True
        manager.iptables_cmd_ns_prefix = {"": []}
        manager.lock = {"": threading.Lock()}
        manager.num_changes = {"": 0}
        manager.update_thread = {"": None}
        manager.bfdAllowed = False
        manager.VxlanAllowed = False
        manager.VxlanSrcIP = ""
        manager.MUX_CABLE_TABLE = "MUX_CABLE_TABLE"
        manager.BFD_SESSION_TABLE = "BFD_SESSION_TABLE"
        manager.VXLAN_TUNNEL_TABLE = "VXLAN_TUNNEL_TABLE"
        manager.ACL_TABLE = "ACL_TABLE"
        manager.ACL_RULE = "ACL_RULE"
        manager.ACL_TABLE_TYPE_CTRLPLANE = "CTRLPLANE"

        # Mocking methods
        manager.update_control_plane_acls = MagicMock()
        manager.allow_bfd_protocol = MagicMock()
        manager.allow_vxlan_port = MagicMock()
        manager.block_vxlan_port = MagicMock()
        manager.update_dhcp_acl_for_mark_change = MagicMock()
        manager.update_dhcp_acl = MagicMock()
        manager.setup_dhcp_chain = MagicMock()

        manager.run()

        # Asserting the method calls
        manager.update_control_plane_acls.assert_called()
        manager.allow_bfd_protocol.assert_not_called()
        manager.allow_vxlan_port.assert_not_called()
        manager.block_vxlan_port.assert_not_called()
        manager.update_dhcp_acl_for_mark_change.assert_not_called()
        manager.update_dhcp_acl.assert_not_called()
        manager.setup_dhcp_chain.assert_called()
