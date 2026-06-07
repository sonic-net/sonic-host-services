import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_redfish_acl_vectors import REDFISH_ACL_TEST_VECTOR, REDFISH_ACL_DISABLED_FEATURE_VECTORS
from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdRedfishAcl(TestCase):
    """
        Verifies that an ACL_TABLE referencing the REDFISH service produces
        iptables rules on tcp/443. Guards against the ACL_SERVICES["REDFISH"]
        entry being removed or renamed.
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    @parameterized.expand(REDFISH_ACL_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_redfish_acl(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        self.assertTrue(caclmgrd_daemon.RedfishAllowed,
                        "Seed at __init__ should set RedfishAllowed=True for these vectors")

        self.assertIn("REDFISH", caclmgrd_daemon.ACL_SERVICES,
                      "ACL_SERVICES is missing the 'REDFISH' entry — see scripts/caclmgrd")
        self.assertEqual(caclmgrd_daemon.ACL_SERVICES["REDFISH"]["dst_ports"], ["443"])
        self.assertEqual(caclmgrd_daemon.ACL_SERVICES["REDFISH"]["ip_protocols"], ["tcp"])

        iptables_rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        test_data['return'] = [tuple(i) for i in test_data['return']]
        iptables_rules_ret = [tuple(i) for i in iptables_rules_ret]
        self.assertEqual(set(test_data["return"]).issubset(set(iptables_rules_ret)), True,
                         "Expected iptables rules not produced. Got: {}".format(iptables_rules_ret))

    @parameterized.expand(REDFISH_ACL_DISABLED_FEATURE_VECTORS)
    @patchfs
    def test_caclmgrd_redfish_acl_skipped_when_feature_disabled(self, test_name, test_data, fs):
        """
            REDFISH only ships on BMC-equipped platforms. When the redfish FEATURE
            is "disabled", RedfishAllowed stays False and an operator template
            referencing the REDFISH service must not program any tcp/443 iptables
            rules -- otherwise it would silently govern whatever else (if anything)
            listens on 443. Parametrized over IPv4 and IPv6 to confirm the gate
            skips REDFISH regardless of IP family.
        """
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        self.assertFalse(caclmgrd_daemon.RedfishAllowed,
                         "Seed at __init__ should set RedfishAllowed=False when FEATURE.redfish.state=disabled")

        iptables_rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())

        port_443_rules = [r for r in iptables_rules_ret if "--dport" in r and "443" in r]
        self.assertEqual(port_443_rules, test_data["return"],
                         "REDFISH ACL should be skipped when RedfishAllowed=False, "
                         "but found tcp/443 rules: {}".format(port_443_rules))

    @patchfs
    def test_handle_redfish_feature_events(self, fs):
        """
            Drive handle_redfish_feature_events (the FEATURE-table subscription
            handler) directly, covering every branch: enable transition, disable
            transition, non-redfish event ignored, and same-state no-op.
        """
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db({
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {"redfish": {"state": "disabled"}},
        })
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        self.assertFalse(caclmgrd_daemon.RedfishAllowed)

        def sub_with(events):
            sub = mock.MagicMock()
            sub.pop.side_effect = events + [("", None, None)]
            return sub

        # enable: flag flips True and namespace queued for re-walk
        notif = set()
        caclmgrd_daemon.handle_redfish_feature_events(
            sub_with([("redfish", "SET", (("state", "enabled"),))]), "", notif)
        self.assertTrue(caclmgrd_daemon.RedfishAllowed)
        self.assertIn("", notif)

        # disable: flag flips False and namespace queued
        notif = set()
        caclmgrd_daemon.handle_redfish_feature_events(
            sub_with([("redfish", "SET", (("state", "disabled"),))]), "", notif)
        self.assertFalse(caclmgrd_daemon.RedfishAllowed)
        self.assertIn("", notif)

        # non-redfish FEATURE event: ignored, nothing queued
        notif = set()
        caclmgrd_daemon.handle_redfish_feature_events(
            sub_with([("snmp", "SET", (("state", "enabled"),))]), "", notif)
        self.assertFalse(caclmgrd_daemon.RedfishAllowed)
        self.assertEqual(notif, set())

        # same-state event (already disabled): no-op, nothing queued
        notif = set()
        caclmgrd_daemon.handle_redfish_feature_events(
            sub_with([("redfish", "SET", (("state", "disabled"),))]), "", notif)
        self.assertFalse(caclmgrd_daemon.RedfishAllowed)
        self.assertEqual(notif, set())
