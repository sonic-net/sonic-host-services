import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_chassis_midplane_vectors import CACLMGRD_CHASSIS_MIDPLANE_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


class TestCaclmgrdChassisMidplane(TestCase):
    """
        Test caclmgrd Chassis Midplane
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

    @parameterized.expand(CACLMGRD_CHASSIS_MIDPLANE_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_chassis_midplane(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH) # fake database_config.json

        mock_is_chassis = mock.MagicMock(return_value=True)
        mock_is_smartswitch = mock.MagicMock(return_value=False)

        with mock.patch("sonic_py_common.device_info.is_chassis", mock_is_chassis):
            with mock.patch("sonic_py_common.device_info.is_smartswitch", mock_is_smartswitch):
                with mock.patch("caclmgrd.ControlPlaneAclManager.run_commands_pipe", side_effect=["eth1-midplane", "1.0.0.33", "eth1-midplane", "1.0.0.33"]):
                        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
                        config_db_connector = caclmgrd_daemon.config_db_map['']
                        ret = caclmgrd_daemon.generate_allow_internal_chasis_midplane_traffic('', config_db_connector)
                        self.assertListEqual(test_data["return"], ret)
                        ret = caclmgrd_daemon.generate_allow_internal_chasis_midplane_traffic('asic0', config_db_connector)
                        self.assertListEqual([], ret)
                        mock_is_chassis.return_value = False
                        mock_is_smartswitch.return_value = True
                        # Mock the get_midplane_bridge_ip_from_configdb method for smartswitch test
                        with mock.patch.object(caclmgrd_daemon, 'get_midplane_bridge_ip_from_configdb', return_value="169.254.200.254"):
                            ret = caclmgrd_daemon.generate_allow_internal_chasis_midplane_traffic('', config_db_connector)
                            self.assertListEqual(test_data["return_smartswitch"], ret)

    def test_get_midplane_bridge_ip_from_configdb(self):
        """Test the get_midplane_bridge_ip_from_configdb method"""
        # Mock ConfigDB data with required tables
        mock_config_db = {
            "DEVICE_METADATA": {
                "localhost": {
                    "subtype": "ToR"
                }
            },
            "FEATURE": {
                "feature1": {
                    "state": "enabled"
                }
            },
            "MID_PLANE_BRIDGE": {
                "GLOBAL": {
                    "ip_prefix": "169.254.200.254/24"
                }
            }
        }
        
        # Set up the mock ConfigDB
        config_db_connector = MockConfigDb()
        config_db_connector.set_config_db(mock_config_db)
        
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        
        # Test with valid ConfigDB data
        ip_address = caclmgrd_daemon.get_midplane_bridge_ip_from_configdb(config_db_connector)
        self.assertEqual(ip_address, "169.254.200.254")
        
        # Test with different IP prefix format
        mock_config_db["MID_PLANE_BRIDGE"]["GLOBAL"]["ip_prefix"] = "10.0.0.1/16"
        config_db_connector.mod_config_db(mock_config_db)
        ip_address = caclmgrd_daemon.get_midplane_bridge_ip_from_configdb(config_db_connector)
        self.assertEqual(ip_address, "10.0.0.1")
        
        # Test with missing ConfigDB data (should return default)
        mock_config_db["MID_PLANE_BRIDGE"]["GLOBAL"] = {}
        config_db_connector.mod_config_db(mock_config_db)
        ip_address = caclmgrd_daemon.get_midplane_bridge_ip_from_configdb(config_db_connector)
        self.assertEqual(ip_address, "169.254.200.254")