import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_external_client_acl_vectors import EXTERNAL_CLIENT_ACL_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = "/var/run/redis/sonic-db/database_config.json"


class TestCaclmgrdExternalClientAcl(TestCase):
    """
    Test caclmgrd EXTERNAL_CLIENT_ACL
    """

    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, "caclmgrd")
        self.caclmgrd = load_module_from_source("caclmgrd", caclmgrd_path)

    @parameterized.expand(EXTERNAL_CLIENT_ACL_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_external_client_acl(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(
            return_value=[]
        )
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(
            return_value=["INPUT", "FORWARD", "OUTPUT"]
        )
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = (
            mock.MagicMock(return_value="")
        )
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        iptables_rules_ret, _ = (
            caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
                "", MockConfigDb()
            )
        )
        test_data["return"] = [tuple(i) for i in test_data["return"]]
        iptables_rules_ret = [tuple(i) for i in iptables_rules_ret]
        self.assertEqual(
            set(test_data["return"]).issubset(set(iptables_rules_ret)), True
        )
        # Assert no duplicate iptables rules are emitted (SONIC-103)
        self.assertEqual(len(iptables_rules_ret), len(set(iptables_rules_ret)))
        caclmgrd_daemon.iptables_cmd_ns_prefix["asic0"] = [
            "ip",
            "netns",
            "exec",
            "asic0",
        ]
        caclmgrd_daemon.namespace_docker_mgmt_ip["asic0"] = "1.1.1.1"
        caclmgrd_daemon.namespace_mgmt_ip = "2.2.2.2"
        caclmgrd_daemon.namespace_docker_mgmt_ipv6["asic0"] = "fd::01"
        caclmgrd_daemon.namespace_mgmt_ipv6 = "fd::02"

        _ = caclmgrd_daemon.generate_fwd_traffic_from_namespace_to_host_commands(
            "asic0", None
        )

    @patchfs
    def test_acl_services_not_polluted_across_reloads(self, fs):
        """
        BUG 2 regression test: ACL_SERVICES class-level dict must not be mutated
        with dst_ports from a previous call. If it were, a port deleted from
        Config DB between two reloads would still appear in the second call's
        iptables rules (stale port leak).

        Scenario:
          - First call: EXTERNAL_CLIENT table has rules for ports 8081 and 8082.
          - Second call (simulated reload): only port 8081 remains.
          - Expected: second call produces no rules for port 8082.
        """
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(
            return_value=[]
        )
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(
            return_value=["INPUT", "FORWARD", "OUTPUT"]
        )
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = (
            mock.MagicMock(return_value="")
        )
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")

        # First reload: ports 8081 and 8082 both present
        config_first = {
            "ACL_TABLE": {
                "EXTERNAL_CLIENT_ACL": {
                    "stage": "INGRESS",
                    "type": "CTRLPLANE",
                    "services": ["EXTERNAL_CLIENT"],
                }
            },
            "ACL_RULE": {
                "EXTERNAL_CLIENT_ACL|DEFAULT_RULE": {
                    "ETHER_TYPE": "2048",
                    "PACKET_ACTION": "DROP",
                    "PRIORITY": "1",
                },
                "EXTERNAL_CLIENT_ACL|RULE_1": {
                    "L4_DST_PORT": "8081",
                    "PACKET_ACTION": "ACCEPT",
                    "PRIORITY": "9998",
                    "SRC_IP": "10.0.0.1/32",
                },
                "EXTERNAL_CLIENT_ACL|RULE_2": {
                    "L4_DST_PORT": "8082",
                    "PACKET_ACTION": "ACCEPT",
                    "PRIORITY": "9997",
                    "SRC_IP": "10.0.0.1/32",
                },
            },
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        }
        MockConfigDb.set_config_db(config_first)
        rules_first, _ = (
            caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
                "", MockConfigDb()
            )
        )
        rules_first = [tuple(r) for r in rules_first]
        self.assertIn(
            ("iptables", "-A", "INPUT", "-p", "tcp", "--dport", "8081", "-j", "DROP"),
            rules_first,
        )
        self.assertIn(
            ("iptables", "-A", "INPUT", "-p", "tcp", "--dport", "8082", "-j", "DROP"),
            rules_first,
        )

        # Second reload: RULE_2 (port 8082) deleted from Config DB
        config_second = {
            "ACL_TABLE": {
                "EXTERNAL_CLIENT_ACL": {
                    "stage": "INGRESS",
                    "type": "CTRLPLANE",
                    "services": ["EXTERNAL_CLIENT"],
                }
            },
            "ACL_RULE": {
                "EXTERNAL_CLIENT_ACL|DEFAULT_RULE": {
                    "ETHER_TYPE": "2048",
                    "PACKET_ACTION": "DROP",
                    "PRIORITY": "1",
                },
                "EXTERNAL_CLIENT_ACL|RULE_1": {
                    "L4_DST_PORT": "8081",
                    "PACKET_ACTION": "ACCEPT",
                    "PRIORITY": "9998",
                    "SRC_IP": "10.0.0.1/32",
                },
            },
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        }
        MockConfigDb.set_config_db(config_second)
        rules_second, _ = (
            caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
                "", MockConfigDb()
            )
        )
        rules_second = [tuple(r) for r in rules_second]

        # Port 8081 must still be present
        self.assertIn(
            ("iptables", "-A", "INPUT", "-p", "tcp", "--dport", "8081", "-j", "DROP"),
            rules_second,
        )
        # Port 8082 must NOT appear — it was deleted and ACL_SERVICES must not carry stale state
        port_8082_rules = [r for r in rules_second if "--dport" in r and "8082" in r]
        self.assertEqual(
            port_8082_rules,
            [],
            "Stale port 8082 rules found after reload — ACL_SERVICES class dict was polluted",
        )
