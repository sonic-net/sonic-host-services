import os
import sys
import swsscommon
from unittest.mock import call, patch
from unittest import TestCase, mock
from tests.common.mock_configdb import MockConfigDb
from sonic_py_common.general import load_module_from_source
import threading
import time
import sys

from queue import Queue
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

    @patch('caclmgrd.ControlPlaneAclManager.update_control_plane_acls')
    def test_update_control_plane_acls_exception(self, mock_update):
        # Set the side effect to raise an exception
        mock_update.side_effect = Exception('Test exception')
        # Mock the necessary attributes and methods
        manager = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
        manager.UPDATE_DELAY_SECS = 1
        manager.lock = {'': threading.Lock()}
        manager.num_changes = {'': 0}
        manager.update_thread = {'': None}
        exception_queue = Queue()
        manager.num_changes[''] = 1
        manager.check_and_update_control_plane_acls('', 0, exception_queue)
        self.assertFalse(exception_queue.empty())
        exc_info = exception_queue.get()
        self.assertEqual(exc_info[0], '')
        self.assertIn('Test exception', exc_info[1])
