import os
import sys

from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'
BMC_DATA = {
    "bmc_if_name": "bmc0",
    "bmc_if_addr": "169.254.100.2",
    "bmc_addr": "169.254.100.1",
    "bmc_net_mask": "255.255.255.252"
}


class TestCaclmgrdBmcLink(TestCase):
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        sys.path.insert(0, modules_path)
        self.caclmgrd = load_module_from_source('caclmgrd', os.path.join(modules_path, "scripts", "caclmgrd"))

    def _daemon(self, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)
        return self.caclmgrd.ControlPlaneAclManager("caclmgrd")

    @patchfs
    def test_no_bmc_data(self, fs):
        daemon = self._daemon(fs)
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value=None, create=True):
            self.assertListEqual([], daemon.generate_allow_bmc_link_traffic(''))

    @patchfs
    def test_allow_bmc_link_interface(self, fs):
        daemon = self._daemon(fs)
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value=BMC_DATA, create=True):
            expected = [['iptables', '-A', 'INPUT', '-i', 'bmc0', '-j', 'ACCEPT']]
            self.assertListEqual(expected, daemon.generate_allow_bmc_link_traffic(''))
            self.assertListEqual([], daemon.generate_allow_bmc_link_traffic('asic0'))

    @patchfs
    def test_bmc_data_without_interface(self, fs):
        daemon = self._daemon(fs)
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value={"bmc_addr": "169.254.100.1"}, create=True):
            self.assertListEqual([], daemon.generate_allow_bmc_link_traffic(''))

    def _generate_full_rules(self, fs):
        MockConfigDb.set_config_db({
            "ACL_RULE": {},
            "ACL_TABLE": {},
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        })
        daemon_cls = self.caclmgrd.ControlPlaneAclManager
        daemon_cls.get_namespace_mgmt_ip = mock.MagicMock()
        daemon_cls.get_namespace_mgmt_ipv6 = mock.MagicMock()
        daemon_cls.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        daemon_cls.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        daemon_cls.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        daemon = self._daemon(fs)
        rules, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        return [tuple(r) for r in rules]

    @patchfs
    def test_bmc_link_rule_precedes_established_rule(self, fs):
        bmc_rule = ('iptables', '-A', 'INPUT', '-i', 'bmc0', '-j', 'ACCEPT')
        established_rule = ('iptables', '-A', 'INPUT', '-m', 'conntrack', '--ctstate',
                            'ESTABLISHED,RELATED', '-j', 'ACCEPT')
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value=BMC_DATA, create=True):
            rules = self._generate_full_rules(fs)
        self.assertEqual(1, rules.count(bmc_rule))
        self.assertLess(rules.index(bmc_rule), rules.index(established_rule))

    @patchfs
    def test_no_bmc_link_rule_without_bmc_data(self, fs):
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value=None, create=True):
            rules = self._generate_full_rules(fs)
        self.assertFalse([r for r in rules if '-i' in r and 'bmc0' in r])
