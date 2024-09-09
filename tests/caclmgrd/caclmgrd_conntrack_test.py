import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs
from sonic_py_common.general import load_module_from_source

from .test_icmpv6_ct_rule_vectors import CACLMGRD_ICMPV6_CT_RULE_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdConntrack(TestCase):
    """
    Test to verify that ip6tables rules to not track icmpv6 packets exist
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)
        self.icmpv6_ct_prerouting_rule = ('ip6tables', '-t', 'raw', '-A', 'PREROUTING', '-p', 'ipv6-icmp', '-j', 'NOTRACK')
        self.icmpv6_ct_output_rule = ('ip6tables', '-t', 'raw', '-A', 'OUTPUT', '-p', 'ipv6-icmp', '-j', 'NOTRACK')

    @parameterized.expand(CACLMGRD_ICMPV6_CT_RULE_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_icmpv6_ct_rules(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        ip6tables_rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        ip6tables_rules_ret = [tuple(i) for i in ip6tables_rules_ret]
        self.assertIn(self.icmpv6_ct_prerouting_rule, ip6tables_rules_ret)
        self.assertIn(self.icmpv6_ct_output_rule, ip6tables_rules_ret)
