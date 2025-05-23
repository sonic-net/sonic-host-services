import os
import sys
import time
import signal
import psutil
import swsscommon as swsscommon_package
from subprocess import CalledProcessError
from sonic_py_common import device_info
from swsscommon import swsscommon
from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock

from .test_vectors import HOSTCFG_DAEMON_INIT_CFG_DB, HOSTCFG_DAEMON_CFG_DB
from tests.common.mock_configdb import MockConfigDb, MockDBConnector
from pyfakefs.fake_filesystem_unittest import patchfs
from deepdiff import DeepDiff
from unittest.mock import call

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

# Load the file under test
hostcfgd_path = os.path.join(scripts_path, 'hostcfgd')
hostcfgd = load_module_from_source('hostcfgd', hostcfgd_path)
hostcfgd.ConfigDBConnector = MockConfigDb
hostcfgd.DBConnector = MockDBConnector
hostcfgd.Table = mock.Mock()


class TesNtpCfgd(TestCase):
    """
        Test hostcfd daemon - NtpCfgd
    """
    def setUp(self):
        MockConfigDb.CONFIG_DB['NTP'] = {
            'global': {'vrf': 'mgmt', 'src_intf': 'eth0'}
        }
        MockConfigDb.CONFIG_DB['NTP_SERVER'] = {'0.debian.pool.ntp.org': {}}
        MockConfigDb.CONFIG_DB['NTP_KEY'] = {'42': {'value': 'theanswer'}}

    def tearDown(self):
        MockConfigDb.CONFIG_DB = {}

    def test_ntp_update_ntp_keys(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.ntp_global_update(
                'global', MockConfigDb.CONFIG_DB['NTP']['global'])
            mocked_subprocess.check_call.assert_has_calls([
                call(['systemctl', 'restart', 'chrony'])
            ])

            mocked_subprocess.check_call.reset_mock()
            ntpcfgd.ntp_srv_key_update({}, MockConfigDb.CONFIG_DB['NTP_KEY'])
            mocked_subprocess.check_call.assert_has_calls([
                call(['systemctl', 'restart', 'chrony'])
            ])

    def test_ntp_global_update_ntp_servers(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.ntp_global_update(
                'global', MockConfigDb.CONFIG_DB['NTP']['global'])
            mocked_subprocess.check_call.assert_has_calls([
                call(['systemctl', 'restart', 'chrony'])
            ])

            mocked_subprocess.check_call.reset_mock()
            ntpcfgd.ntp_srv_key_update({'0.debian.pool.ntp.org': {}}, {})
            mocked_subprocess.check_call.assert_has_calls([
                call(['systemctl', 'restart', 'chrony'])
            ])

    def test_ntp_is_caching_config(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.cache['global'] = MockConfigDb.CONFIG_DB['NTP']['global']
            ntpcfgd.cache['servers'] = MockConfigDb.CONFIG_DB['NTP_SERVER']
            ntpcfgd.cache['keys'] = MockConfigDb.CONFIG_DB['NTP_KEY']

            ntpcfgd.ntp_global_update(
                'global', MockConfigDb.CONFIG_DB['NTP']['global'])
            ntpcfgd.ntp_srv_key_update(MockConfigDb.CONFIG_DB['NTP_SERVER'],
                                       MockConfigDb.CONFIG_DB['NTP_KEY'])

            mocked_subprocess.check_call.assert_not_called()

    def test_loopback_update(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.cache['global'] = MockConfigDb.CONFIG_DB['NTP']['global']
            ntpcfgd.cache['servers'] = {'0.debian.pool.ntp.org': {}}

            ntpcfgd.handle_ntp_source_intf_chg('eth0')
            mocked_subprocess.check_call.assert_has_calls([
                call(['systemctl', 'restart', 'chrony'])
            ])


class TestSerialConsoleCfgd(TestCase):
    """
        Test hostcfd daemon - SerialConsoleCfg
    """
    def setUp(self):
        MockConfigDb.CONFIG_DB['SERIAL_CONSOLE'] = {
            'POLICIES': {'inactivity-timeout': '15', 'sysrq-capabilities': 'disabled'}
        }

    def tearDown(self):
        MockConfigDb.CONFIG_DB = {}

    def test_serial_console_update_cfg(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            serialcfg = hostcfgd.SerialConsoleCfg()
            serialcfg.update_serial_console_cfg(
                'POLICIES', MockConfigDb.CONFIG_DB['SERIAL_CONSOLE']['POLICIES'])
            mocked_subprocess.check_call.assert_has_calls([
                call(['sudo', 'service', 'serial-config', 'restart']),
            ])

    def test_serial_console_is_caching_config(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            serialcfg = hostcfgd.SerialConsoleCfg()
            serialcfg.cache['POLICIES'] = MockConfigDb.CONFIG_DB['SERIAL_CONSOLE']['POLICIES']

            serialcfg.update_serial_console_cfg(
                'POLICIES', MockConfigDb.CONFIG_DB['SERIAL_CONSOLE']['POLICIES'])

            mocked_subprocess.check_call.assert_not_called()


class TestHostcfgdDaemon(TestCase):

    def setUp(self):
        self.get_dev_meta = mock.patch(
            'sonic_py_common.device_info.get_device_runtime_metadata',
            return_value={'DEVICE_RUNTIME_METADATA': {}})
        self.get_dev_meta.start()
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)

    def tearDown(self):
        MockConfigDb.CONFIG_DB = {}
        self.get_dev_meta.stop()

    def test_loopback_events(self):
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        MockConfigDb.event_queue = [('NTP', 'global'),
                                  ('NTP_SERVER', '0.debian.pool.ntp.org'),
                                  ('LOOPBACK_INTERFACE', 'Loopback0|10.184.8.233/32')]
        daemon = hostcfgd.HostConfigDaemon()
        daemon.register_callbacks()
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            try:
                daemon.start()
            except TimeoutError:
                pass
            expected = [call(['systemctl', 'restart', 'chrony']),
            call(['iptables', '-t', 'mangle', '--append', 'PREROUTING', '-p', 'tcp', '--tcp-flags', 'SYN', 'SYN', '-d', '10.184.8.233', '-j', 'TCPMSS', '--set-mss', '1460']),
            call(['iptables', '-t', 'mangle', '--append', 'POSTROUTING', '-p', 'tcp', '--tcp-flags', 'SYN', 'SYN', '-s', '10.184.8.233', '-j', 'TCPMSS', '--set-mss', '1460'])]
            mocked_subprocess.check_call.assert_has_calls(expected, any_order=True)

    def test_kdump_event(self):
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        daemon = hostcfgd.HostConfigDaemon()
        daemon.register_callbacks()
        MockConfigDb.event_queue = [('KDUMP', 'config')]
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            try:
                daemon.start()
            except TimeoutError:
                pass
            expected = [
                call(['sonic-kdump-config', '--disable']),
                call(['sonic-kdump-config', '--num_dumps', '3']),
                call(['sonic-kdump-config', '--memory', '0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M']),
                call(['sonic-kdump-config', '--remote', 'false']),  # Covering remote
                call(['sonic-kdump-config', '--ssh_string', 'user@localhost']),  # Covering ssh_string
                call(['sonic-kdump-config', '--ssh_path', '/a/b/c'])  # Covering ssh_path
            ]
            mocked_subprocess.check_call.assert_has_calls(expected, any_order=True)

    def test_kdump_load(self):
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_INIT_CFG_DB)
        MockConfigDb.CONFIG_DB['KDUMP'] = {
            'config': {
                "enabled": "true",
            }
        }
        daemon = hostcfgd.HostConfigDaemon()
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            daemon.kdumpCfg.load(MockConfigDb.CONFIG_DB['KDUMP'])

            expected = [
                call(['sonic-kdump-config', '--enable']),
                call(['sonic-kdump-config', '--num_dumps', '3']),
                call(['sonic-kdump-config', '--memory', '0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M']),
                call(['sonic-kdump-config', '--remote', 'false']),  # Covering remote
                call(['sonic-kdump-config', '--ssh_string', 'user@localhost']),  # Covering ssh_string
                call(['sonic-kdump-config', '--ssh_path', '/a/b/c'])  # Covering ssh_path
            ]

            mocked_subprocess.check_call.assert_has_calls(expected, any_order=True)

    def test_kdump_event_with_proc_cmdline(self):
        os.environ["HOSTCFGD_UNIT_TESTING"] = "2"
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        daemon = hostcfgd.HostConfigDaemon()
        default=daemon.kdumpCfg.kdump_defaults
        daemon.kdumpCfg.load(default)
        daemon.register_callbacks()
        MockConfigDb.event_queue = [('KDUMP', 'config')]
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            try:
                daemon.start()
            except TimeoutError:
                pass
            expected = [
                call(['sonic-kdump-config', '--enable']),
                call(['sonic-kdump-config', '--num_dumps', '3']),
                call(['sonic-kdump-config', '--memory', '8G-:1G']),
                call(['sonic-kdump-config', '--remote', 'false']),  # Covering remote
                call(['sonic-kdump-config', '--ssh_string', 'user@localhost']),  # Covering ssh_string
                call(['sonic-kdump-config', '--ssh_path', '/a/b/c'])  # Covering ssh_path
            ]
            mocked_subprocess.check_call.assert_has_calls(expected, any_order=True)
        os.environ["HOSTCFGD_UNIT_TESTING"] = ""
        
    def test_devicemeta_event(self):
        """
        Test handling DEVICE_METADATA events.
        1) Hostname reload
        1) Timezone reload
        1) syslog_with_osversion flag change
        """
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        MockConfigDb.event_queue = [(swsscommon.CFG_DEVICE_METADATA_TABLE_NAME,
                                    'localhost')]
        daemon = hostcfgd.HostConfigDaemon()
        daemon.aaacfg = mock.MagicMock()
        daemon.iptables = mock.MagicMock()
        daemon.passwcfg = mock.MagicMock()
        daemon.dnscfg = mock.MagicMock()
        daemon.load(HOSTCFG_DAEMON_INIT_CFG_DB)
        daemon.register_callbacks()
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            try:
                daemon.start()
            except TimeoutError:
                pass

            expected = [
                call(['sudo', 'service', 'hostname-config', 'restart']),
                call(['sudo', 'monit', 'reload']),
                call(['timedatectl', 'set-timezone', 'Europe/Kyiv']),
                call(['systemctl', 'restart', 'rsyslog']),
            ]
            mocked_subprocess.check_call.assert_has_calls(expected,
                                                          any_order=True)

        # Mock empty name
        HOSTCFG_DAEMON_CFG_DB["DEVICE_METADATA"]["localhost"]["hostname"] = ""
        original_syslog = hostcfgd.syslog
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        with mock.patch('hostcfgd.syslog') as mocked_syslog:
            with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
                mocked_syslog.LOG_ERR = original_syslog.LOG_ERR
                try:
                    daemon.start()
                except TimeoutError:
                    pass

                expected = [
                    call(original_syslog.LOG_ERR, 'Hostname was not updated: Empty not allowed')
                ]
                mocked_syslog.syslog.assert_has_calls(expected)

        daemon.devmetacfg.hostname = "SameHostName"
        HOSTCFG_DAEMON_CFG_DB["DEVICE_METADATA"]["localhost"]["hostname"] = daemon.devmetacfg.hostname
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        with mock.patch('hostcfgd.syslog') as mocked_syslog:
            with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
                mocked_syslog.LOG_INFO = original_syslog.LOG_INFO
                try:
                    daemon.start()
                except TimeoutError:
                    pass

                expected = [
                    call(original_syslog.LOG_INFO, 'Hostname was not updated: Already set up with the same name: SameHostName')
                ]
                mocked_syslog.syslog.assert_has_calls(expected)

        daemon.devmetacfg.syslog_with_osversion = "false"
        HOSTCFG_DAEMON_CFG_DB["DEVICE_METADATA"]["localhost"]["syslog_with_osversion"] = 'true'
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        with mock.patch('hostcfgd.syslog') as mocked_syslog:
            with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
                mocked_syslog.LOG_INFO = original_syslog.LOG_INFO
                try:
                    daemon.start()
                except TimeoutError:
                    pass

                expected = [
                    call(original_syslog.LOG_INFO, 'DeviceMetaCfg: Restart rsyslog-config after feature flag change to true')
                ]
                mocked_syslog.syslog.assert_has_calls(expected)

    def test_mgmtiface_event(self):
        """
        Test handling mgmt events.
        1) Management interface setup
        2) Management vrf setup
        """
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        MockConfigDb.event_queue = [
            (swsscommon.CFG_MGMT_INTERFACE_TABLE_NAME, 'eth0|1.2.3.4/24'),
            (swsscommon.CFG_MGMT_VRF_CONFIG_TABLE_NAME, 'vrf_global')
        ]
        daemon = hostcfgd.HostConfigDaemon()
        daemon.register_callbacks()
        daemon.aaacfg = mock.MagicMock()
        daemon.iptables = mock.MagicMock()
        daemon.passwcfg = mock.MagicMock()
        daemon.dnscfg = mock.MagicMock()
        daemon.load(HOSTCFG_DAEMON_INIT_CFG_DB)
        with mock.patch('hostcfgd.check_output_pipe') as mocked_check_output:
            with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
                popen_mock = mock.Mock()
                attrs = {'communicate.return_value': ('output', 'error')}
                popen_mock.configure_mock(**attrs)
                mocked_subprocess.Popen.return_value = popen_mock

                try:
                    daemon.start()
                except TimeoutError:
                    pass

                expected = [
                    call(['sudo', 'systemctl', 'restart', 'interfaces-config']),
                    call(['systemctl', 'stop', 'chrony']),
                    call(['systemctl', 'restart', 'interfaces-config']),
                    call(['systemctl', 'start', 'chrony']),
                    call(['ip', '-4', 'route', 'del', 'default', 'dev', 'eth0', 'metric', '202'])
                ]
                mocked_subprocess.check_call.assert_has_calls(expected)
                expected = [
                    call(['cat', '/proc/net/route'], ['grep', '-E', r"eth0\s+00000000\s+[0-9A-Z]+\s+[0-9]+\s+[0-9]+\s+[0-9]+\s+202"], ['wc', '-l'])
                ]
                mocked_check_output.assert_has_calls(expected)

    def test_mgmtvrf_route_check_failed(self):
        mgmtiface = hostcfgd.MgmtIfaceCfg()
        mgmtiface.load({}, {'mgmtVrfEnabled' : "false"})
        with mock.patch('hostcfgd.check_output_pipe') as mocked_check_output:
            with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
                popen_mock = mock.Mock()
                attrs = {'communicate.return_value': ('output', 'error')}
                popen_mock.configure_mock(**attrs)
                mocked_subprocess.Popen.return_value = popen_mock
                mocked_subprocess.CalledProcessError = CalledProcessError
                # Simulate the case where there is no default route
                mocked_check_output.side_effect = CalledProcessError(returncode=1, cmd="", output="")

                mgmtiface.update_mgmt_vrf({'mgmtVrfEnabled' : "true"})
                expected = [
                    call(['cat', '/proc/net/route'], ['grep', '-E', r"eth0\s+00000000\s+[0-9A-Z]+\s+[0-9]+\s+[0-9]+\s+[0-9]+\s+202"], ['wc', '-l'])
                ]
                mocked_check_output.assert_has_calls(expected)
                assert mgmtiface.mgmt_vrf_enabled == "true"

                mgmtiface.update_mgmt_vrf({'mgmtVrfEnabled' : "false"})
                assert mgmtiface.mgmt_vrf_enabled == "false"

    def test_dns_events(self):
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        MockConfigDb.event_queue = [('DNS_NAMESERVER', '1.1.1.1')]
        daemon = hostcfgd.HostConfigDaemon()
        daemon.register_callbacks()
        with mock.patch('hostcfgd.run_cmd') as mocked_run_cmd:
            try:
                daemon.start()
            except TimeoutError:
                pass
            mocked_run_cmd.assert_has_calls([call(['systemctl', 'restart', 'resolv-config'], True, False)])


class TestDnsHandler:

    @mock.patch('hostcfgd.run_cmd')
    def test_dns_update(self, mock_run_cmd):
        dns_cfg = hostcfgd.DnsCfg()
        key = "1.1.1.1"
        dns_cfg.dns_update(key, {})

        mock_run_cmd.assert_has_calls([call(['systemctl', 'restart', 'resolv-config'], True, False)])

    def test_load(self):
        dns_cfg = hostcfgd.DnsCfg()
        dns_cfg.dns_update = mock.MagicMock()

        data = {}
        dns_cfg.load(data)
        dns_cfg.dns_update.assert_called()


class TestBannerCfg:
    def test_load(self):
        banner_cfg = hostcfgd.BannerCfg()
        banner_cfg.banner_message = mock.MagicMock()

        data = {}
        banner_cfg.load(data)
        banner_cfg.banner_message.assert_called()

    @mock.patch('hostcfgd.run_cmd')
    def test_banner_message(self, mock_run_cmd):
        banner_cfg = hostcfgd.BannerCfg()
        banner_cfg.banner_message(None, {'test': 'test'})

        mock_run_cmd.assert_has_calls([call(['systemctl', 'restart', 'banner-config'], True, True)])


class TestMemoryStatisticsCfgd(TestCase):
    """Test suite for MemoryStatisticsCfg class which handles memory statistics configuration and daemon management."""

    def setUp(self):
        """Set up test environment before each test case."""
        MockConfigDb.CONFIG_DB['MEMORY_STATISTICS'] = {
            'memory_statistics': {
                'enabled': 'false',
                'sampling_interval': '5',
                'retention_period': '15'
            }
        }
        self.mem_stat_cfg = hostcfgd.MemoryStatisticsCfg(MockConfigDb.CONFIG_DB)

    def tearDown(self):
        """Clean up after each test case."""
        MockConfigDb.CONFIG_DB = {}

    # Group 1: Configuration Loading Tests
    def test_load_with_invalid_key(self):
        """
        Test loading configuration with an invalid key.
        Ensures the system properly logs when encountering unknown configuration parameters.
        """
        config = {'invalid_key': 'value', 'enabled': 'true'}
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.load(config)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Invalid key 'invalid_key' in initial configuration.")

    def test_load_with_empty_config(self):
        """
        Test loading an empty configuration.
        Verifies system behavior when no configuration is provided.
        """
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.load(None)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Loading initial configuration")

    # Group 2: Configuration Update Tests
    def test_memory_statistics_update_invalid_key(self):
        """
        Test updating configuration with an invalid key.
        Ensures system properly handles and logs attempts to update non-existent configuration parameters.
        """
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.memory_statistics_update('invalid_key', 'value')
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Invalid key 'invalid_key' received.")

    def test_memory_statistics_update_invalid_numeric_value(self):
        """
        Test updating numeric configuration with invalid value.
        Verifies system properly validates numeric input parameters.
        """
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.memory_statistics_update('sampling_interval', '-1')
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Invalid value '-1' for key 'sampling_interval'. Must be a positive integer.")

    def test_memory_statistics_update_same_value(self):
        """
        Test updating configuration with the same value.
        Ensures system doesn't perform unnecessary updates when value hasn't changed.
        """
        with mock.patch.object(self.mem_stat_cfg, 'apply_setting') as mock_apply:
            self.mem_stat_cfg.memory_statistics_update('sampling_interval', '5')
            mock_apply.assert_not_called()

    # Group 3: Daemon Management Tests
    @mock.patch('hostcfgd.subprocess.Popen')
    @mock.patch('hostcfgd.os.kill')
    def test_restart_memory_statistics_success(self, mock_kill, mock_popen):
        """
        Test successful restart of the memory statistics daemon.
        Verifies proper shutdown of existing process and startup of new process.
        """
        with mock.patch('hostcfgd.syslog.syslog'):
            with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123):
                self.mem_stat_cfg.restart_memory_statistics()
                mock_kill.assert_called_with(123, signal.SIGTERM)
                mock_popen.assert_called_once()

    @mock.patch('hostcfgd.subprocess.Popen')
    def test_restart_memory_statistics_failure(self, mock_popen):
        """
        Test failed restart of memory statistics daemon.
        Ensures proper error handling when daemon fails to start.
        """
        mock_popen.side_effect = Exception("Failed to start")
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.restart_memory_statistics()
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Failed to start MemoryStatisticsDaemon: Failed to start")

    # Group 4: PID Management Tests
    def test_get_memory_statistics_pid_success(self):
        """
        Test successful retrieval of daemon PID.
        Verifies proper PID retrieval when daemon is running correctly.
        """
        mock_process = mock.Mock()
        mock_process.name.return_value = "memory_statistics_service.py"

        with mock.patch('builtins.open', mock.mock_open(read_data="123")), \
             mock.patch('hostcfgd.psutil.pid_exists', return_value=True), \
             mock.patch('hostcfgd.psutil.Process', return_value=mock_process):
            pid = self.mem_stat_cfg.get_memory_statistics_pid()
            self.assertEqual(pid, 123)

    def test_get_memory_statistics_pid_file_not_found(self):
        """
        Test PID retrieval when PID file doesn't exist.
        Ensures proper handling of missing PID file.
        """
        with mock.patch('builtins.open', side_effect=FileNotFoundError):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                pid = self.mem_stat_cfg.get_memory_statistics_pid()
                self.assertIsNone(pid)
                mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: PID file not found. Daemon might not be running.")

    def test_get_memory_statistics_pid_invalid_content(self):
        """
        Test PID retrieval when PID file contains invalid content.
        Ensures proper handling and error logging when PID file is corrupted or contains non-numeric data.
        """
        mock_open = mock.mock_open(read_data="invalid")
        with mock.patch('builtins.open', mock_open):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                pid = self.mem_stat_cfg.get_memory_statistics_pid()
                self.assertIsNone(pid)
                mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: PID file contents invalid.")

    @mock.patch('hostcfgd.psutil.pid_exists', return_value=True)
    @mock.patch('hostcfgd.psutil.Process')
    def test_get_memory_statistics_pid_wrong_process(self, mock_process, mock_pid_exists):
        """
        Test PID retrieval when process exists but name doesn't match expected daemon name.
        Verifies proper handling when PID belongs to a different process than the memory statistics daemon.
        """
        mock_process_instance = mock.Mock()
        mock_process_instance.name.return_value = "wrong_process"
        mock_process.return_value = mock_process_instance

        mock_open = mock.mock_open(read_data="123")
        with mock.patch('builtins.open', mock_open):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                pid = self.mem_stat_cfg.get_memory_statistics_pid()
                self.assertIsNone(pid)
                mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: PID 123 does not correspond to memory_statistics_service.py.")

    @mock.patch('hostcfgd.psutil.pid_exists', return_value=False)
    def test_get_memory_statistics_pid_nonexistent(self, mock_pid_exists):
        """Test get_memory_statistics_pid when PID doesn't exist"""
        mock_open = mock.mock_open(read_data="123")
        with mock.patch('builtins.open', mock_open):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                pid = self.mem_stat_cfg.get_memory_statistics_pid()
                self.assertIsNone(pid)
                mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: PID does not exist.")

    # Group 5: Enable/Disable Tests
    def test_memory_statistics_enable(self):
        """
        Test enabling memory statistics functionality.
        Verifies proper activation of memory statistics monitoring.
        """
        with mock.patch.object(self.mem_stat_cfg, 'restart_memory_statistics') as mock_restart:
            self.mem_stat_cfg.memory_statistics_update('enabled', 'true')
            mock_restart.assert_called_once()
            self.assertEqual(self.mem_stat_cfg.cache['enabled'], 'true')

    def test_apply_setting_with_non_enabled_key(self):
        """Test apply_setting with sampling_interval or retention_period"""
        with mock.patch.object(self.mem_stat_cfg, 'reload_memory_statistics') as mock_reload:
            self.mem_stat_cfg.apply_setting('sampling_interval', '10')
            mock_reload.assert_called_once()

    def test_apply_setting_with_enabled_false(self):
        """Test apply_setting with enabled=false"""
        with mock.patch.object(self.mem_stat_cfg, 'shutdown_memory_statistics') as mock_shutdown:
            self.mem_stat_cfg.apply_setting('enabled', 'false')
            mock_shutdown.assert_called_once()

    def test_memory_statistics_disable(self):
        """
        Test disabling memory statistics functionality.
        Ensures proper deactivation of memory statistics monitoring.
        """
        self.mem_stat_cfg.cache['enabled'] = 'true'
        with mock.patch.object(self.mem_stat_cfg, 'apply_setting') as mock_apply:
            self.mem_stat_cfg.memory_statistics_update('enabled', 'false')
            mock_apply.assert_called_once_with('enabled', 'false')
            self.assertEqual(self.mem_stat_cfg.cache['enabled'], 'false')

    def test_memory_statistics_disable_with_shutdown(self):
        """Test disabling memory statistics with full shutdown chain"""
        self.mem_stat_cfg.cache['enabled'] = 'true'

        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123) as mock_get_pid, \
             mock.patch('hostcfgd.os.kill') as mock_kill, \
             mock.patch.object(self.mem_stat_cfg, 'wait_for_shutdown') as mock_wait:

            self.mem_stat_cfg.memory_statistics_update('enabled', 'false')

            mock_get_pid.assert_called_once()
            mock_kill.assert_called_once_with(123, signal.SIGTERM)
            mock_wait.assert_called_once_with(123)

            self.assertEqual(self.mem_stat_cfg.cache['enabled'], 'false')

    def test_memory_statistics_disable_no_running_daemon(self):
        """Test disabling memory statistics when daemon is not running"""
        self.mem_stat_cfg.cache['enabled'] = 'true'

        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=None) as mock_get_pid:
            self.mem_stat_cfg.memory_statistics_update('enabled', 'false')

            mock_get_pid.assert_called_once()

            self.assertEqual(self.mem_stat_cfg.cache['enabled'], 'false')

    # Group 6: Reload Tests
    def test_reload_memory_statistics_success(self):
        """
        Test successful reload of memory statistics configuration.
        Verifies proper handling of configuration updates without restart.
        """
        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123), \
             mock.patch('hostcfgd.os.kill') as mock_kill, \
             mock.patch('hostcfgd.syslog.syslog'):
            self.mem_stat_cfg.reload_memory_statistics()
            mock_kill.assert_called_once_with(123, signal.SIGHUP)

    def test_reload_memory_statistics_no_pid(self):
        """
        Test reload when daemon is not running.
        Ensures proper handling of reload request when daemon is inactive.
        """
        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=None), \
             mock.patch('hostcfgd.os.kill') as mock_kill:
            self.mem_stat_cfg.reload_memory_statistics()
            mock_kill.assert_not_called()

    def test_reload_memory_statistics_failure(self):
        """Test reload failure with exception"""
        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123) as mock_get_pid, \
            mock.patch('hostcfgd.os.kill', side_effect=Exception("Test error")), \
            mock.patch('hostcfgd.syslog.syslog') as mock_syslog:

            self.mem_stat_cfg.reload_memory_statistics()

            mock_get_pid.assert_called_once()
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Failed to reload MemoryStatisticsDaemon: Test error")

    # Group 7: Shutdown Tests
    def test_shutdown_memory_statistics_success(self):
        """
        Test successful shutdown of memory statistics daemon.
        Verifies proper termination of the daemon process.
        """
        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123), \
             mock.patch('hostcfgd.os.kill') as mock_kill, \
             mock.patch.object(self.mem_stat_cfg, 'wait_for_shutdown'), \
             mock.patch('hostcfgd.syslog.syslog'):
            self.mem_stat_cfg.shutdown_memory_statistics()
            mock_kill.assert_called_once_with(123, signal.SIGTERM)

    def test_wait_for_shutdown_timeout(self):
        """
        Test shutdown behavior when daemon doesn't respond to termination signal.
        Ensures proper handling of timeout during shutdown.
        """
        mock_process = mock.Mock()
        mock_process.wait.side_effect = psutil.TimeoutExpired(123, 10)
        with mock.patch('hostcfgd.psutil.Process', return_value=mock_process), \
             mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.wait_for_shutdown(123)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Timed out while waiting for daemon (PID 123) to shut down.")

    @mock.patch('hostcfgd.psutil.Process')
    def test_wait_for_shutdown_no_process(self, mock_process):
        """Test shutdown waiting when process doesn't exist"""
        mock_process.side_effect = psutil.NoSuchProcess(123)

        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.wait_for_shutdown(123)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: MemoryStatisticsDaemon process not found.")

    def test_shutdown_memory_statistics_failure(self):
        """Test shutdown failure with exception"""
        with mock.patch.object(self.mem_stat_cfg, 'get_memory_statistics_pid', return_value=123) as mock_get_pid, \
            mock.patch('hostcfgd.os.kill', side_effect=Exception("Test error")), \
            mock.patch('hostcfgd.syslog.syslog') as mock_syslog:

            self.mem_stat_cfg.shutdown_memory_statistics()

            mock_get_pid.assert_called_once()
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Failed to shutdown MemoryStatisticsDaemon: Test error")

    def test_wait_for_shutdown_success(self):
        """Test successful wait for shutdown"""
        mock_process = mock.Mock()
        with mock.patch('hostcfgd.psutil.Process', return_value=mock_process) as mock_process_class, \
            mock.patch('hostcfgd.syslog.syslog') as mock_syslog:

            self.mem_stat_cfg.wait_for_shutdown(123)

            mock_process_class.assert_called_once_with(123)
            mock_process.wait.assert_called_once_with(timeout=10)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: MemoryStatisticsDaemon stopped gracefully")

    # Group 8: Error Handling Tests
    def test_memory_statistics_update_exception_handling(self):
        """
        Test exception handling during configuration updates.
        Verifies proper error handling and logging of exceptions.
        """
        with mock.patch.object(self.mem_stat_cfg, 'apply_setting', side_effect=Exception("Test error")), \
             mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.memory_statistics_update('enabled', 'true')
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: Failed to manage MemoryStatisticsDaemon: Test error")

    def test_apply_setting_exception(self):
        """Test exception handling in apply_setting"""
        with mock.patch.object(self.mem_stat_cfg, 'restart_memory_statistics',
                             side_effect=Exception("Test error")):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                self.mem_stat_cfg.apply_setting('enabled', 'true')
                mock_syslog.assert_any_call(mock.ANY,
                    "MemoryStatisticsCfg: Exception in apply_setting() for key 'enabled': Test error")

    @mock.patch('hostcfgd.psutil.Process')
    def test_get_memory_statistics_pid_exception(self, mock_process):
        """Test general exception handling in get_memory_statistics_pid"""
        mock_process.side_effect = Exception("Unexpected error")
        mock_open = mock.mock_open(read_data="123")

        with mock.patch('hostcfgd.psutil.pid_exists', return_value=True):
            with mock.patch('builtins.open', mock_open):
                with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                    pid = self.mem_stat_cfg.get_memory_statistics_pid()
                    self.assertIsNone(pid)
                    mock_syslog.assert_any_call(mock.ANY,
                        "MemoryStatisticsCfg: Exception failed to retrieve MemoryStatisticsDaemon PID: Unexpected error")

    def test_memory_statistics_handler_exception(self):
        """Test exception handling in memory_statistics_handler"""
        daemon = hostcfgd.HostConfigDaemon()
        with mock.patch.object(daemon.memorystatisticscfg, 'memory_statistics_update',
                             side_effect=Exception("Handler error")):
            with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
                daemon.memory_statistics_handler('enabled', None, 'true')
                mock_syslog.assert_any_call(mock.ANY,
                    "MemoryStatisticsCfg: Error while handling memory statistics update: Handler error")

    @mock.patch('hostcfgd.psutil.Process')
    def test_wait_for_shutdown_general_exception(self, mock_process):
        """Test general exception handling in wait_for_shutdown"""
        mock_process.side_effect = Exception("Unexpected shutdown error")
        with mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            self.mem_stat_cfg.wait_for_shutdown(123)
            mock_syslog.assert_any_call(mock.ANY,
                "MemoryStatisticsCfg: Exception in wait_for_shutdown(): Unexpected shutdown error")

    def test_process_name_mismatch(self):
        """
        Test handling of process name mismatches.
        Ensures proper validation of daemon process identity.
        """
        mock_process = mock.Mock()
        mock_process.name.return_value = "wrong_process_name"

        with mock.patch('builtins.open', mock.mock_open(read_data="123")), \
             mock.patch('hostcfgd.psutil.pid_exists', return_value=True), \
             mock.patch('hostcfgd.psutil.Process', return_value=mock_process), \
             mock.patch('hostcfgd.syslog.syslog') as mock_syslog:
            pid = self.mem_stat_cfg.get_memory_statistics_pid()
            self.assertIsNone(pid)
            mock_syslog.assert_any_call(mock.ANY, "MemoryStatisticsCfg: PID 123 does not correspond to memory_statistics_service.py.")
