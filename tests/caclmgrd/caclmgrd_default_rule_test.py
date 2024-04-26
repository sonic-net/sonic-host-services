import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_default_rule_vectors import CACLMGRD_DEFAULT_RULE_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdDefaultRule(TestCase):
    """
    Test caclmgrd default deny rule
    1. Default deny rule NOT EXISTS when there is no CACL rule.
    2. Default deny rule EXISTS when there is at least one CACL rules.
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

        self.default_deny_rule_v4 = ('iptables', '-A', 'INPUT', '-j', 'DROP')
        self.default_deny_rule_v6 = ('ip6tables', '-A', 'INPUT', '-j', 'DROP')

    @parameterized.expand(CACLMGRD_DEFAULT_RULE_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_default_rule(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        iptables_rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        iptables_rules_ret = [tuple(i) for i in iptables_rules_ret]
        if test_data['default_deny']:
            self.assertIn(self.default_deny_rule_v4, iptables_rules_ret)
            self.assertIn(self.default_deny_rule_v6, iptables_rules_ret)
        else:
            self.assertNotIn(self.default_deny_rule_v4, iptables_rules_ret)
            self.assertNotIn(self.default_deny_rule_v6, iptables_rules_ret)
