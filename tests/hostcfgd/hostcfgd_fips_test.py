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

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
hostcfgd_path = os.path.join(scripts_path, 'hostcfgd')
loader = importlib.machinery.SourceFileLoader('hostcfgd', hostcfgd_path)
spec = importlib.util.spec_from_loader(loader.name, loader)
hostcfgd = importlib.util.module_from_spec(spec)
loader.exec_module(hostcfgd)
sys.modules['hostcfgd'] = hostcfgd
original_syslog = hostcfgd.syslog

# Mock swsscommon classes
hostcfgd.ConfigDBConnector = MockConfigDb
hostcfgd.DBConnector = MockDBConnector
hostcfgd.Table = mock.Mock()
running_services = [{"unit":"ssh.service","load":"loaded","active":"active","sub":"running","description":"OpenBSD Secure Shell server"},
            {"unit":"restapi.service","load":"loaded","active":"active","sub":"running","description":"SONiC Restful API Service"}]

class TestHostcfgdFIPS(TestCase):
    """
        Test hostcfd daemon - FIPS
    """
    def run_diff(self, file1, file2):
        _, output = getstatusoutput_noshell(['diff', '-uR', file1, file2])
        return output

    def setUp(self):
        self._workPath =os.path.join('/tmp/test_fips/', self._testMethodName)
        self.test_data = {'DEVICE_METADATA':{},'FIPS': {}}
        self.test_data['DEVICE_METADATA'] = {'localhost': {'hostname': 'fips'}}
        self.test_data['FIPS']['global'] = {'enable': 'false', 'enforce': 'false'}
        hostcfgd.FIPS_CONFIG_FILE = os.path.join(self._workPath + 'eips.json')
        hostcfgd.OPENSSL_FIPS_CONFIG_FILE = os.path.join(self._workPath, 'fips_enabled')
        hostcfgd.PROC_CMDLINE = os.path.join(self._workPath, 'cmdline')
        os.makedirs(self._workPath, exist_ok=True)
        with open(hostcfgd.PROC_CMDLINE, 'w') as f:
            f.write('swiotlb=65536 sonic_fips=0')
        with open(hostcfgd.OPENSSL_FIPS_CONFIG_FILE, 'w') as f:
            f.write('0')

    def tearDown(self):
        shutil.rmtree(self._workPath, ignore_errors=True)

    def assert_fips_runtime_config(self, result='1'):
        with open(hostcfgd.OPENSSL_FIPS_CONFIG_FILE) as f:
            assert f.read() == result

    @mock.patch('sonic_installer.bootloader.get_bootloader', side_effect=[MockBootloader()])
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_output', side_effect=[json.dumps(running_services)])
    @mock.patch('subprocess.check_call')
    def test_hostcfgd_fips_enable(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        with open(hostcfgd.PROC_CMDLINE, 'w') as f:
            f.write('swiotlb=65536 sonic_fips=0')
        self.test_data['FIPS']['global']['enable'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.fips_config_handler("FIPS", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service ssh.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service restapi.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: skipped to configure the enforce option False, since the config has already been set.')
        mock_syslog.assert_called_with(original_syslog.LOG_DEBUG, 'FipsCfg: update fips option complete.')
        self.assert_fips_runtime_config()

    @mock.patch('sonic_installer.bootloader.get_bootloader', side_effect=[MockBootloader()])
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_output', side_effect=[json.dumps(running_services)])
    @mock.patch('subprocess.check_call')
    def test_hostcfgd_fips_disable(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        with open(hostcfgd.PROC_CMDLINE, 'w') as f:
            f.write('swiotlb=65536 sonic_fips=0')
        with open(hostcfgd.OPENSSL_FIPS_CONFIG_FILE, 'w') as f:
            f.write('1')
        self.test_data['FIPS']['global']['enable'] = 'false'
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.fips_config_handler("FIPS", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service ssh.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service restapi.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: skipped to configure the enforce option False, since the config has already been set.')
        mock_syslog.assert_called_with(original_syslog.LOG_DEBUG, 'FipsCfg: update fips option complete.')
        self.assert_fips_runtime_config('0')

    @mock.patch('sonic_installer.bootloader.get_bootloader', return_value=MockBootloader())
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_output', side_effect=[json.dumps(running_services)])
    @mock.patch('subprocess.check_call')
    def test_hostcfgd_fips_enforce(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        with open(hostcfgd.PROC_CMDLINE, 'w') as f:
            f.write('swiotlb=65536 sonic_fips=0')
        self.test_data['FIPS']['global']['enforce'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.fips_config_handler("FIPS", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service ssh.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: restart service restapi.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: update the FIPS enforce option True.')
        mock_syslog.assert_called_with(original_syslog.LOG_DEBUG, 'FipsCfg: update fips option complete.')
        self.assert_fips_runtime_config()

    @mock.patch('sonic_installer.bootloader.get_bootloader', return_value=MockBootloader(True))
    @mock.patch('syslog.syslog')
    @mock.patch('subprocess.check_output', side_effect=[json.dumps(running_services)])
    @mock.patch('subprocess.check_call')
    def test_hostcfgd_fips_enforce_reconf(self, mock_check_call, mock_check_output, mock_syslog, mock_get_bootloader):
        with open(hostcfgd.PROC_CMDLINE, 'w') as f:
            f.write('swiotlb=65536 sonic_fips=1')
        self.test_data['FIPS']['global']['enforce'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.fips_config_handler("FIPS", '', self.test_data)
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: skipped to restart services, since FIPS enforced.')
        mock_syslog.assert_any_call(original_syslog.LOG_INFO, 'FipsCfg: skipped to configure the enforce option True, since the config has already been set.')
        mock_syslog.assert_called_with(original_syslog.LOG_DEBUG, 'FipsCfg: update fips option complete.')
        self.assert_fips_runtime_config()
