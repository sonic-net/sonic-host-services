import os
import sys

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_internal_docker_ip_traffic_vectors import CACLMGRD_INTERNAL_DOCKER_IP_TEST_VECTOR


class TestCaclmgrdGenerateInternalDockerIp(TestCase):
    """
        Test caclmgrd multi-asic generate internal docker ip allow rule
    """
    def setUp(self):
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)
        self.maxDiff = None

    @parameterized.expand(CACLMGRD_INTERNAL_DOCKER_IP_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_internal_docker_ip_traffic(self, test_name, test_data, fs):
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        caclmgrd_daemon.iptables_cmd_ns_prefix['asic0'] = ['ip', 'netns', 'exec', 'asic0']
        caclmgrd_daemon.namespace_docker_mgmt_ip['asic0'] = '1.1.1.1/32'
        caclmgrd_daemon.namespace_mgmt_ip = '2.2.2.2/32'
        caclmgrd_daemon.namespace_docker_mgmt_ipv6['asic0'] = 'fd::01/128'
        caclmgrd_daemon.namespace_mgmt_ipv6 = 'fd::02/128'

        ret = caclmgrd_daemon.generate_allow_internal_docker_ip_traffic_commands('asic0')
        self.assertListEqual(test_data["return"], ret)

    @patchfs
    def test_empty_mgmt_ip_returns_empty_list(self, fs):
        """
        BUG 7 regression test: when both IPv4 and IPv6 management IPs are empty,
        generate_allow_internal_docker_ip_traffic_commands must return an empty
        list without raising an exception or passing empty strings to iptables.
        """
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        caclmgrd_daemon.iptables_cmd_ns_prefix['asic0'] = ['ip', 'netns', 'exec', 'asic0']
        caclmgrd_daemon.namespace_docker_mgmt_ip['asic0'] = ''
        caclmgrd_daemon.namespace_mgmt_ip = ''
        caclmgrd_daemon.namespace_docker_mgmt_ipv6['asic0'] = ''
        caclmgrd_daemon.namespace_mgmt_ipv6 = ''

        ret = caclmgrd_daemon.generate_allow_internal_docker_ip_traffic_commands('asic0')
        self.assertListEqual([], ret,
            "Expected empty list when all management IPs are unavailable")

    @patchfs
    def test_ipv4_valid_ipv6_empty_skips_only_ipv6_rules(self, fs):
        """
        BUG 7 partial-failure regression test: when IPv4 management IPs are valid
        but IPv6 management IPs are empty, only IPv6 rules must be skipped.
        IPv4 rules must still be generated correctly.
        """
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        caclmgrd_daemon.iptables_cmd_ns_prefix['asic0'] = ['ip', 'netns', 'exec', 'asic0']
        caclmgrd_daemon.namespace_docker_mgmt_ip['asic0'] = '1.1.1.1/32'
        caclmgrd_daemon.namespace_mgmt_ip = '2.2.2.2/32'
        caclmgrd_daemon.namespace_docker_mgmt_ipv6['asic0'] = ''
        caclmgrd_daemon.namespace_mgmt_ipv6 = ''

        ret = caclmgrd_daemon.generate_allow_internal_docker_ip_traffic_commands('asic0')
        ret_cmds = [' '.join(r) for r in ret]
        self.assertTrue(any('iptables' in c and '1.1.1.1' in c for c in ret_cmds),
            "Expected IPv4 rules to be generated when IPv4 mgmt IP is valid")
        self.assertFalse(any('ip6tables' in c for c in ret_cmds),
            "Expected no ip6tables rules when IPv6 mgmt IP is empty")

    @patchfs
    def test_ipv6_valid_ipv4_empty_skips_only_ipv4_rules(self, fs):
        """
        BUG 7 partial-failure regression test: when IPv6 management IPs are valid
        but IPv4 management IPs are empty, only IPv4 rules must be skipped.
        IPv6 rules must still be generated correctly.
        """
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        caclmgrd_daemon.iptables_cmd_ns_prefix['asic0'] = ['ip', 'netns', 'exec', 'asic0']
        caclmgrd_daemon.namespace_docker_mgmt_ip['asic0'] = ''
        caclmgrd_daemon.namespace_mgmt_ip = ''
        caclmgrd_daemon.namespace_docker_mgmt_ipv6['asic0'] = 'fd::01/128'
        caclmgrd_daemon.namespace_mgmt_ipv6 = 'fd::02/128'

        ret = caclmgrd_daemon.generate_allow_internal_docker_ip_traffic_commands('asic0')
        ret_cmds = [' '.join(r) for r in ret]
        self.assertTrue(any('ip6tables' in c and 'fd::01' in c for c in ret_cmds),
            "Expected ip6tables rules to be generated when IPv6 mgmt IP is valid")
        self.assertFalse(any(c.split()[4] == 'iptables' for c in ret_cmds),
            "Expected no iptables rules when IPv4 mgmt IP is empty")
