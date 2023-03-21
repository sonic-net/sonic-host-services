import os
import sys
from swsscommon import swsscommon

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_vxlan_vectors import CACLMGRD_VXLAN_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb
from unittest.mock import MagicMock, patch

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

class TestCaclmgrdVxlan(TestCase):
    """
        Test caclmgrd vxlan
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    @parameterized.expand(CACLMGRD_VXLAN_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_vxlan(self, test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH) # fake database_config.json

        MockConfigDb.set_config_db(test_data["config_db"])

        with mock.patch("caclmgrd.ControlPlaneAclManager.run_commands_pipe", return_value='sonic'):
            with mock.patch("caclmgrd.subprocess") as mocked_subprocess:
                popen_mock = mock.Mock()
                popen_attrs = test_data["popen_attributes"]
                popen_mock.configure_mock(**popen_attrs)
                mocked_subprocess.Popen.return_value = popen_mock
                mocked_subprocess.PIPE = -1

                call_rc = test_data["call_rc"]
                mocked_subprocess.call.return_value = call_rc

                caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
                ret = caclmgrd_daemon.allow_vxlan_port('', [])
                assert ret == False
                caclmgrd_daemon.block_vxlan_port('')
                assert ret == False
                data = test_data["input"]
                caclmgrd_daemon.allow_vxlan_port('', data)
                mocked_subprocess.Popen.assert_has_calls(test_data["expected_add_subprocess_calls"], any_order=True)
                caclmgrd_daemon.block_vxlan_port('')
                mocked_subprocess.Popen.assert_has_calls(test_data["expected_del_subprocess_calls"], any_order=True)

