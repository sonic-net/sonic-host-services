import os
import sys
from swsscommon import swsscommon

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from .test_bfd_vectors import CACLMGRD_BFD_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb
from unittest.mock import MagicMock, patch

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

class TestCaclmgrdBfd(TestCase):
    """
        Test caclmgrd bfd
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    @parameterized.expand(CACLMGRD_BFD_TEST_VECTOR)
    @patchfs
    def test_caclmgrd_bfd(self, test_name, test_data, fs):
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
                caclmgrd_daemon.allow_bfd_protocol('')
                mocked_subprocess.Popen.assert_has_calls(test_data["expected_bfd_subprocess_calls"], any_order=True)
                caclmgrd_daemon.bfdAllowed = True
                mocked_subprocess.Popen.reset_mock()
                caclmgrd_daemon.num_changes[''] = 1
                caclmgrd_daemon.check_and_update_control_plane_acls('', 1)

                #Ensure BFD rules are installed before ip2me rules to avoid traffic loss during update of control plane acl rules
                bfd_ipv4_idx = 0
                bfd_ipv6_idx = 0
                ip2me_ipv4_idx = 0
                ip2me_ipv6_idx = 0
                idx = 0
                for call in mocked_subprocess.Popen.call_args_list:
                    if test_data["expected_subprocess_calls"][0] == call:
                        bfd_ipv4_idx = idx
                    elif test_data["expected_subprocess_calls"][1] == call:
                        bfd_ipv6_idx = idx
                    elif test_data["expected_subprocess_calls"][2] == call:
                        ip2me_ipv4_idx = idx
                    elif test_data["expected_subprocess_calls"][3] == call:
                        ip2me_ipv6_idx = idx
                    idx = idx+1
                assert (bfd_ipv4_idx != 0 and bfd_ipv6_idx != 0 and ip2me_ipv4_idx !=0 and ip2me_ipv6_idx != 0)
                assert (bfd_ipv4_idx < ip2me_ipv4_idx and bfd_ipv6_idx < ip2me_ipv6_idx)

