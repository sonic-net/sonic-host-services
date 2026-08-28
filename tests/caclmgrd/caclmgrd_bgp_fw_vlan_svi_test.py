import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_bgp_fw_vlan_svi_vectors import CACLMGRD_BGP_FW_VLAN_SVI_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdBgpFwVlanSvi(TestCase):
    """
        Test caclmgrd BGP-to-FW-VLAN-SVI deny logic (Option 1)
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

    @parameterized.expand(CACLMGRD_BGP_FW_VLAN_SVI_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_bgp_fw_vlan_svi(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)  # fake database_config.json

        MockConfigDb.set_config_db(test_data["config_db"])
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        ret = caclmgrd_daemon.generate_block_bgp_to_fw_vlan_svi_commands('', MockConfigDb())
        self.assertListEqual(test_data["return"], ret)
