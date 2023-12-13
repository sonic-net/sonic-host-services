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
