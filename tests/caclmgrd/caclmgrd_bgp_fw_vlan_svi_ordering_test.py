import os
import sys

from swsscommon import swsscommon
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs
from sonic_py_common.general import load_module_from_source

from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdBgpFwVlanSviOrdering(TestCase):
    """
    Verify the BGP-to-FW-VLAN-SVI DROP rule (ingress-interface match) is
    positioned ahead of the ESTABLISHED,RELATED ACCEPT rule and the
    blanket BGP ACCEPT rule in the full generated ruleset, so that
    follow-up packets of an established VLAN-sourced BGP session are
    also blocked (not just the initial SYN), and so the DROP is not
    shadowed by the blanket ACCEPT.
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)
        self.maxDiff = None

    @patchfs
    def test_bgp_svi_drop_precedes_established_related_and_blanket_accept(self, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        MockConfigDb.set_config_db({
            "DEVICE_METADATA": {
                "localhost": {
                    "mgmt_type": "FairWater",
                    "type": "BackEndToRRouter",
                }
            },
        })
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())

        def find_index(predicate):
            for i, cmd in enumerate(ret):
                if predicate(cmd):
                    return i
            return None

        bgp_svi_drop_idx = find_index(lambda c: '-i' in c and 'Vlan+' in c and '179' in c and 'DROP' in c)
        established_related_idx = find_index(lambda c: 'ESTABLISHED,RELATED' in c)
        blanket_bgp_accept_idx = find_index(lambda c: '179' in c and 'ACCEPT' in c and 'Vlan+' not in c)

        self.assertIsNotNone(bgp_svi_drop_idx, "Expected a BGP-to-VLAN-SVI DROP rule (-i Vlan+) in the generated ruleset")
        self.assertIsNotNone(established_related_idx, "Expected an ESTABLISHED,RELATED ACCEPT rule in the generated ruleset")
        self.assertIsNotNone(blanket_bgp_accept_idx, "Expected the blanket BGP ACCEPT rule in the generated ruleset")

        self.assertLess(bgp_svi_drop_idx, established_related_idx,
            "BGP-to-VLAN-SVI DROP rule must precede ESTABLISHED,RELATED ACCEPT, "
            "otherwise follow-up packets of an established VLAN-sourced BGP "
            "session would bypass the DROP")
        self.assertLess(bgp_svi_drop_idx, blanket_bgp_accept_idx,
            "BGP-to-VLAN-SVI DROP rule must precede the blanket BGP ACCEPT rule")
