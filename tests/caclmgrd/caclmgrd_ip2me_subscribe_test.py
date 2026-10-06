import os
import sys
import threading

import swsscommon
from unittest.mock import patch, MagicMock
from unittest import TestCase

from tests.common.mock_configdb import MockConfigDb
from sonic_py_common.general import load_module_from_source


DBCONFIG_PATH = "/var/run/redis/sonic-db/database_config.json"

IP2ME_INTERFACE_TABLES = [
    "LOOPBACK_INTERFACE",
    "VLAN_INTERFACE",
    "PORTCHANNEL_INTERFACE",
    "INTERFACE",
]


class TestCaclmgrdIP2MeSubscribe(TestCase):
    """caclmgrd must re-render control plane ACLs when interface addressing changes.

    The "block_ip2me" rules are derived from the interface tables. Without a
    subscription to those tables, removing an interface address never triggers a
    re-render and the DROP rule for the removed address survives in iptables.
    """

    def setUp(self):
        swsscommon.swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        self.caclmgrd = load_module_from_source(
            "caclmgrd", os.path.join(scripts_path, "caclmgrd"))

    def _make_manager(self, mock_swsscommon, pop_values, db_ids):
        """Drive run() once over a scripted set of subscriber events."""
        mock_swsscommon.SonicDBConfig.getDbId.side_effect = lambda db_name: (
            6 if db_name == "STATE_DB" else 1
        )
        mock_swsscommon.DBConnector.side_effect = [MagicMock(), MagicMock(), MagicMock()]
        mock_swsscommon.Select.OBJECT = 1
        mock_swsscommon.Select.return_value.select.return_value = (
            mock_swsscommon.Select.OBJECT, MagicMock())
        mock_swsscommon.SubscriberStateTable.return_value.getTableNameSeparator.return_value = "|"
        mock_swsscommon.SubscriberStateTable.return_value.pop.side_effect = pop_values
        cast = mock_swsscommon.CastSelectableToRedisSelectObj.return_value
        cast.getDbConnector.return_value.getNamespace.return_value = ""
        cast.getDbConnector.return_value.getDbId.side_effect = db_ids

        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = MagicMock()
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        manager.log_info = MagicMock()
        manager.log_error = MagicMock()
        manager.DualToR = False
        manager.iptables_cmd_ns_prefix = {"": []}
        manager.lock = {"": threading.Lock()}
        manager.num_changes = {"": 0}
        # Pre-populated so run() does not spawn a real update thread.
        manager.update_thread = {"": threading.Thread()}
        manager.thread_exceptions = {}
        manager.update_control_plane_acls = MagicMock()

        try:
            manager.run()
        except StopIteration:
            # run() loops forever; exhausting the scripted events ends the test.
            pass
        return manager

    @patch("caclmgrd.swsscommon")
    @patch("os.geteuid", return_value=0)
    def test_interface_tables_are_subscribed(self, mock_geteuid, mock_swsscommon):
        self._make_manager(mock_swsscommon, [(None, None, None)] * 64, [1])

        subscribed = [c[0][1] for c in mock_swsscommon.SubscriberStateTable.call_args_list
                      if len(c[0]) > 1]
        for table_name in IP2ME_INTERFACE_TABLES:
            self.assertIn(table_name, subscribed)

    @patch("caclmgrd.swsscommon")
    @patch("os.geteuid", return_value=0)
    def test_interface_address_event_triggers_rerender(self, mock_geteuid, mock_swsscommon):
        # Ordered to match run()'s drain sequence for a CONFIG_DB event: vxlan, dpu,
        # feature, the two ACL subscribers, then the four interface subscribers. The
        # interface key carries an address, so it contains the table separator - the
        # ACL handler would have discarded it as an unknown ACL table.
        pop_values = [
            (None, None, None),                            # vxlan
            (None, None, None),                            # dpu
            (None, None, None),                            # feature
            (None, None, None),                            # ACL_TABLE
            (None, None, None),                            # ACL_RULE
            ("Ethernet0|10.0.0.1/31", "DEL", []),          # INTERFACE-family subscriber
            (None, None, None),
        ] + [(None, None, None)] * 8

        manager = self._make_manager(mock_swsscommon, pop_values, [1])

        self.assertEqual(manager.num_changes[""], 1)
