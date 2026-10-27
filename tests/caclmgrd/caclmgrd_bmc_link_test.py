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
        MockConfigDb.set_config_db({
            "ACL_RULE": {},
            "ACL_TABLE": {},
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        })

    def _daemon(self, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)
        # The constructor runs real "ip" commands, which cannot run under pyfakefs.
        daemon_cls = self.caclmgrd.ControlPlaneAclManager
        for name in ("get_namespace_mgmt_ip", "get_namespace_mgmt_ipv6"):
            patcher = mock.patch.object(daemon_cls, name, return_value='')
            patcher.start()
            self.addCleanup(patcher.stop)
        return daemon_cls("caclmgrd")

    def _generate(self, bmc_data, namespace=''):
        # The method does not use self, so the daemon does not need to be built.
        with mock.patch("sonic_py_common.device_info.get_bmc_data", return_value=bmc_data, create=True):
            return self.caclmgrd.ControlPlaneAclManager.generate_allow_bmc_link_traffic(mock.MagicMock(), namespace)

    def test_no_bmc_data(self):
        self.assertListEqual([], self._generate(None))

    def test_allow_bmc_link_interface(self):
        expected = [['iptables', '-A', 'INPUT', '-i', 'bmc0', '-j', 'ACCEPT']]
        self.assertListEqual(expected, self._generate(BMC_DATA))
        self.assertListEqual([], self._generate(BMC_DATA, 'asic0'))

    def test_allow_bmc_link_interface_on_bmc_side(self):
        # The same bmc.json is used on the switch host and on the BMC, so the
        # rule must be generated from the interface name alone, with no role.
        expected = [['iptables', '-A', 'INPUT', '-i', 'bmc0', '-j', 'ACCEPT']]
        self.assertListEqual(expected, self._generate({"bmc_if_name": "bmc0"}))

    def test_bmc_data_without_interface(self):
        self.assertListEqual([], self._generate({"bmc_addr": "169.254.100.1"}))

    def _generate_full_rules(self, fs):
        daemon_cls = self.caclmgrd.ControlPlaneAclManager
        for name, value in (("generate_block_ip2me_traffic_iptables_commands", []),
                            ("get_chain_list", ["INPUT", "FORWARD", "OUTPUT"]),
                            ("get_chassis_midplane_interface_ip", '')):
            patcher = mock.patch.object(daemon_cls, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
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
