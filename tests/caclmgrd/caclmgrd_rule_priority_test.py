import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_rule_priority_vectors import CACLMGRD_RULE_PRIORITY_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdRulePriority(TestCase):
    """
    Test that caclmgrd installs control plane ACL rules in descending
    numeric order of PRIORITY.
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

    def create_daemon(self, config_db):
        MockConfigDb.set_config_db(config_db)
        manager = self.caclmgrd.ControlPlaneAclManager
        manager.get_namespace_mgmt_ip = mock.MagicMock()
        manager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        manager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        manager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        manager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        return manager("caclmgrd")

    @staticmethod
    def get_acl_rule_commands(caclmgrd_daemon):
        iptables_cmds, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        # Only the commands built from ACL_RULE entries match on both a source
        # address and a destination port
        return [cmd for cmd in iptables_cmds if '-s' in cmd and '--dport' in cmd]

    @parameterized.expand(CACLMGRD_RULE_PRIORITY_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_rule_priority(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        caclmgrd_daemon = self.create_daemon(test_data["config_db"])
        self.assertEqual(self.get_acl_rule_commands(caclmgrd_daemon), test_data["return"])

    @patchfs
    def test_caclmgrd_rule_non_integer_priority_skipped(self, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        caclmgrd_daemon = self.create_daemon({
            "ACL_TABLE": {
                "SSH_ONLY": {
                    "stage": "ingress",
                    "type": "CTRLPLANE",
                    "services": ["SSH"]
                }
            },
            "ACL_RULE": {
                "SSH_ONLY|RULE_1": {
                    "PACKET_ACTION": "ACCEPT",
                    "PRIORITY": "9999",
                    "SRC_IP": "10.0.0.1/32"
                },
                "SSH_ONLY|RULE_BAD": {
                    "PACKET_ACTION": "DROP",
                    "PRIORITY": "high",
                    "SRC_IP": "10.0.0.2/32"
                },
            },
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        })
        caclmgrd_daemon.log_error = mock.MagicMock()

        self.assertEqual(self.get_acl_rule_commands(caclmgrd_daemon), [
            ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.1/32', '--dport', '22', '-j', 'ACCEPT'],
        ])
        caclmgrd_daemon.log_error.assert_any_call(
            "rule_props for rule_id RULE_BAD has a non-integer PRIORITY 'high'!")
