import importlib.machinery
import importlib.util
import filecmp
import json
import shutil
import os
import sys
from swsscommon import swsscommon

from parameterized import parameterized
from unittest import TestCase, mock
from tests.common.mock_configdb import MockConfigDb, MockDBConnector
from tests.common.mock_bootloader import MockBootloader
from sonic_py_common.general import getstatusoutput_noshell


test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
bmpcfgd_path = os.path.join(scripts_path, 'bmpcfgd')
bmpcfgd = load_module_from_source('bmpcfgd', bmpcfgd_path)


original_syslog = bmpcfgd.syslog

# Mock swsscommon classes
bmpcfgd.ConfigDBConnector = MockConfigDb
bmpcfgd.DBConnector = MockDBConnector
bmpcfgd.Table = mock.Mock()


class TestBMPCfgDaemon(TestCase):
    """
        Test bmpcfgd daemon
    """
    def setUp(self):
        self.test_data['BMP']['table'] = {'bgp_neighbor_table': 'false', 'bgp_rib_in_table': 'false', 'bgp_rib_out_table': 'false'}

    @patch('subprocess.call')
    def test_start_bmp(self, mock_call):
        obj = BMPCfg()
        obj.start_bmp()
        mock_call.assert_called_once_with(["service", "openbmpd", "start"])

    @patch('subprocess.call')
    def test_stop_bmp(self, mock_call):
        obj = BMPCfg()
        obj.stop_bmp()
        mock_call.assert_called_once_with(["service", "openbmpd", "stop"])

    @mock.patch('sonic_installer.bootloader.get_bootloader', side_effect=[MockBootloader()])
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_call')
    def test_bmpcfgd_neighbor_enable(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        self.test_data['BMP']['table']['bgp_neighbor_table'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        bmp_config_daemon = bmpcfgd.BMPCfgDaemon()
        bmp_config_daemon.bmp_handler("BMP", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: update : true, false, false')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: stop bmp daemon.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: Reset bmp table from state_db.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: start bmp daemon.')

    @mock.patch('sonic_installer.bootloader.get_bootloader', side_effect=[MockBootloader()])
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_call')
    def test_bmpcfgd_bgp_rib_in_enable(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        self.test_data['BMP']['table']['bgp_rib_in_table'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        bmp_config_daemon = bmpcfgd.BMPCfgDaemon()
        bmp_config_daemon.bmp_handler("BMP", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: update : false, true, false')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: stop bmp daemon.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: Reset bmp table from state_db.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: start bmp daemon.')

    @mock.patch('sonic_installer.bootloader.get_bootloader', side_effect=[MockBootloader()])
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_call')
    def test_bmpcfgd_bgp_rib_out_enable(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        self.test_data['BMP']['table']['bgp_rib_out_table'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        bmp_config_daemon = bmpcfgd.BMPCfgDaemon()
        bmp_config_daemon.bmp_handler("BMP", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: update : false, false, true')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: stop bmp daemon.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: Reset bmp table from state_db.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'BMPCfg: start bmp daemon.')