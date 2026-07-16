import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_gil_ip_multiasic_vectors import (
    CACLMGRD_GIL_IP_MULTIASIC_TEST_VECTOR,
    NAMESPACE_MGMT_IP,
    NAMESPACE_MGMT_IPV6,
    ASIC0_NS_PREFIX,
)
from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdGilIpMultiAsic(TestCase):
    """
    Test caclmgrd GIL IP multi-asic FORWARD chain rules.

    Verifies that for non-default namespaces, connections from the GIL
    (Global In-band Link) management IP are allowed/blocked via the
    FORWARD iptables chain for services with multi_asic_ns_to_host_fwd=True.
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

    def _make_daemon(self):
        """Create a daemon instance with mocked network helpers."""
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock(return_value=NAMESPACE_MGMT_IP)
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock(return_value=NAMESPACE_MGMT_IPV6)
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        # Wire up asic0 namespace
        daemon.iptables_cmd_ns_prefix['asic0'] = ASIC0_NS_PREFIX
        daemon.namespace_mgmt_ip = NAMESPACE_MGMT_IP
        daemon.namespace_mgmt_ipv6 = NAMESPACE_MGMT_IPV6
        daemon.namespace_docker_mgmt_ip['asic0'] = '1.1.1.1'
        daemon.namespace_docker_mgmt_ipv6['asic0'] = 'fd::01'
        return daemon

    @parameterized.expand(CACLMGRD_GIL_IP_MULTIASIC_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_gil_ip_multiasic(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        daemon = self._make_daemon()
        namespace = test_data["namespace"]

        iptables_rules, _ = daemon.get_acl_rules_and_translate_to_iptables_commands(
            namespace, MockConfigDb()
        )

        # Convert to tuples for set operations
        rules_set = set(tuple(r) for r in iptables_rules)

        for rule in test_data.get("expected_present", []):
            self.assertIn(
                tuple(rule), rules_set,
                msg=f"[{test_name}] Expected rule missing: {rule}"
            )

        for rule in test_data.get("expected_absent", []):
            self.assertNotIn(
                tuple(rule), rules_set,
                msg=f"[{test_name}] Unexpected rule found: {rule}"
            )
