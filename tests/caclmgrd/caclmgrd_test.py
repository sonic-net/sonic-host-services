import os
import sys
import swsscommon
from unittest.mock import call, patch, MagicMock
from unittest import TestCase, mock
from tests.common.mock_configdb import MockConfigDb
from sonic_py_common.general import load_module_from_source
import threading
import sys


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
        manager.num_changes[""] = 1
        manager.check_and_update_control_plane_acls("", 0)
        # Assert that thread_exceptions exists and contains the exception
        self.assertTrue(manager.thread_exceptions)
        self.assertIn("", manager.thread_exceptions)
        error, exc_info = manager.thread_exceptions.get("")
        self.assertEqual(error, "Exception('Test exception')")
        self.assertIn("Traceback (most recent call last):", exc_info[0])
        self.assertIn("Test exception", exc_info[-1])


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
            6 if db_name == "STATE_DB" else 1
        )
        mock_kill.return_value = None
        mock_state_db_connector = MagicMock()
        mock_config_db_connector = MagicMock()
        mock_swsscommon.DBConnector.side_effect = [mock_state_db_connector, mock_config_db_connector, mock_state_db_connector]
        mock_swsscommon.Select.OBJECT = 1
        mock_swsscommon.Select.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.Select.return_value.removeSelectable.return_value = MagicMock()

        mock_swsscommon.SubscriberStateTable.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.SubscriberStateTable.return_value.getTableNameSeparator.return_value = "|"
        pop_values = [
            ("key1", "SET", [("mark", "0x11"), ("field2", "value2")]),
            (None, None, None),
            ("key2", "DEL", []),
            (None, None, None),
            ("key3", "SET", [("mark", "0x11")]),
            (None, None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
            ("key5", "SET", [("mark", "0x11"), ("field4", "value4")]),
            ('', None, None),
            ("key6", "SET", [("mark", "0x11"), ("field5", "value5")]),
            ('', None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
        ]
        mock_swsscommon.SubscriberStateTable.return_value.pop.side_effect = pop_values
        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getNamespace.return_value = ""

        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getDbId.side_effect = [6, 4, 4]
        # Creating an instance of ControlPlaneAclManager
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = MagicMock()
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        # Setting necessary attributes
        manager.log_info = MagicMock()
        manager.log_error = MagicMock()
        manager.DualToR = True
        manager.iptables_cmd_ns_prefix = {"": []}
        manager.lock = {"": threading.Lock()}
        manager.num_changes = {"": 2}
        manager.update_thread = {"": None}
        manager.bfdAllowed = False
        manager.VxlanAllowed = True
        manager.VxlanSrcIP = ""
        manager.MUX_CABLE_TABLE = "MUX_CABLE_TABLE"
        manager.BFD_SESSION_TABLE = "BFD_SESSION_TABLE"
        manager.VXLAN_TUNNEL_TABLE = "VXLAN_TUNNEL_TABLE"
        manager.ACL_TABLE = "ACL_TABLE"
        manager.ACL_RULE = "ACL_RULE"
        manager.ACL_TABLE_TYPE_CTRLPLANE = "CTRLPLANE"

        # Mocking methods
        manager.removeSelectable = MagicMock()
        manager.update_control_plane_acls = MagicMock()
        manager.allow_bfd_protocol = MagicMock()
        manager.allow_vxlan_port = MagicMock()
        manager.block_vxlan_port = MagicMock()
        manager.update_dhcp_acl_for_mark_change = MagicMock()
        manager.update_dhcp_acl = MagicMock()
        manager.setup_dhcp_chain = MagicMock()
        try:
            manager.run()
        except StopIteration as e:
            # This is expected to happen
            pass

        # Asserting the method calls
        manager.update_control_plane_acls.assert_called()
        manager.allow_bfd_protocol.assert_called()
        manager.allow_vxlan_port.assert_not_called()
        manager.block_vxlan_port.assert_not_called()
        manager.update_dhcp_acl_for_mark_change.assert_called()
        manager.update_dhcp_acl.assert_called()
        manager.setup_dhcp_chain.assert_called()


    @patch("caclmgrd.swsscommon")
    @patch("os.geteuid", return_value=0)
    @patch("os.kill")
    @patch("signal.SIGKILL", return_value=9)
    @patch("sys.exit")
    def test_run_exception(
        self,
        mock_exit,
        mock_sigkill,
        mock_kill,
        mock_geteuid,
        mock_swsscommon
    ):
        mock_kill.return_value = None
        mock_sigkill.return_value = 9
        mock_geteuid.return_value = 0
        mock_exit.return_value = None

        mock_swsscommon.SonicDBConfig.getDbId.side_effect = lambda db_name: (
            6 if db_name == "STATE_DB" else 1
        )
        mock_state_db_connector = MagicMock()
        mock_config_db_connector = MagicMock()
        mock_swsscommon.DBConnector.side_effect = [mock_state_db_connector, mock_config_db_connector, mock_state_db_connector]
        mock_swsscommon.Select.OBJECT = 1
        mock_swsscommon.Select.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.Select.return_value.removeSelectable.return_value = MagicMock()

        mock_swsscommon.SubscriberStateTable.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT,
            MagicMock(),
        )
        mock_swsscommon.SubscriberStateTable.return_value.getTableNameSeparator.return_value = "|"
        pop_values = [
            ("key1", "SET", [("mark", "0x11"), ("field2", "value2")]),
            (None, None, None),
            ("key2", "DEL", []),
            (None, None, None),
            ("key3", "SET", [("mark", "0x11")]),
            (None, None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
            ("key5", "SET", [("mark", "0x11"), ("field4", "value4")]),
            ('', None, None),
            ("key6", "SET", [("mark", "0x11"), ("field5", "value5")]),
            ('', None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
            ("SSH_ONLY", "SET", (('policy_desc', 'SSH_ONLY'), ('services', 'SSH'), ('stage', 'ingress'), ('type', 'CTRLPLANE'))),
            ('', None, None),
        ]
        mock_swsscommon.SubscriberStateTable.return_value.pop.side_effect = pop_values
        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getNamespace.return_value = ""

        mock_swsscommon.CastSelectableToRedisSelectObj.return_value.getDbConnector.return_value.getDbId.side_effect = [6, 4, 4]

        # Creating an instance of ControlPlaneAclManager
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = MagicMock()
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        # Setting necessary attributes
        manager.log_info = MagicMock()
        manager.log_error = MagicMock()
        manager.DualToR = True
        manager.iptables_cmd_ns_prefix = {"": []}
        manager.lock = {"": threading.Lock()}
        manager.num_changes = {"": 2}
        manager.update_thread = {"": threading.Thread()}
        manager.bfdAllowed = False
        manager.VxlanAllowed = True
        manager.VxlanSrcIP = ""
        manager.MUX_CABLE_TABLE = "MUX_CABLE_TABLE"
        manager.BFD_SESSION_TABLE = "BFD_SESSION_TABLE"
        manager.VXLAN_TUNNEL_TABLE = "VXLAN_TUNNEL_TABLE"
        manager.ACL_TABLE = "ACL_TABLE"
        manager.ACL_RULE = "ACL_RULE"
        manager.ACL_TABLE_TYPE_CTRLPLANE = "CTRLPLANE"

        # Mocking methods
        manager.removeSelectable = MagicMock()
        manager.allow_bfd_protocol = MagicMock()
        manager.update_control_plane_acls = MagicMock()
        manager.allow_vxlan_port = MagicMock()
        manager.block_vxlan_port = MagicMock()
        manager.update_dhcp_acl_for_mark_change = MagicMock()
        manager.update_dhcp_acl = MagicMock()
        manager.setup_dhcp_chain = MagicMock()
        manager.thread_exceptions = {}
        namespace = ""
        error = "Simulated exception"
        exc_info = ["Traceback (most recent call last):", "  File \"mock.py\", line 1, in <module>", "    raise Exception('Simulated exception')"]
        manager.thread_exceptions[namespace] = (error, exc_info)
        try:
            manager.run()
        except StopIteration as e:
            # This is expected to happen
            pass

        # Asserting the method calls
        manager.update_control_plane_acls.assert_called()
        mock_kill.assert_called()
