import os
import sys
import swsscommon
from unittest.mock import call
from unittest import TestCase, mock
from tests.common.mock_configdb import MockConfigDb
from sonic_py_common.general import load_module_from_source

class TestCaclmgrd(TestCase):
    def setUp(self):
        swsscommon.swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    def test_run_commands_pipe(self):
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        output = caclmgrd_daemon.run_commands_pipe(['echo', 'caclmgrd'], ['awk', '{print $1}'])
        assert output == 'caclmgrd'

        output = caclmgrd_daemon.run_commands_pipe([sys.executable, "-c", "import sys; sys.exit(6)"], [sys.executable, "-c", "import sys; sys.exit(8)"])
        assert output == ''

    def test_get_chain_list(self):
        expected_calls = [call(['iptables', '-L', '-v', '-n'], ['grep', 'Chain'], ['awk', '{print $2}'])]
        caclmgrd_daemon = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        with mock.patch("caclmgrd.ControlPlaneAclManager.run_commands_pipe") as mock_run_commands_pipe:
            caclmgrd_daemon.get_chain_list([], [''])
            mock_run_commands_pipe.assert_has_calls(expected_calls)

