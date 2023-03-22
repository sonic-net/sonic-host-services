import os
import sys
import swsscommon as swsscommon_package
from sonic_py_common import device_info
from swsscommon import swsscommon

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock

from .test_vectors import HOSTCFG_DAEMON_INIT_CFG_DB
from .test_vectors import HOSTCFGD_TEST_VECTOR, HOSTCFG_DAEMON_CFG_DB
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

class TestFeatureHandler(TestCase):
    """Test methods of `FeatureHandler` class.
    """
    def checks_config_table(self, feature_table, expected_table):
        """Compares `FEATURE` table in `CONFIG_DB` with expected output table.

        Args:
            feature_table: A dictionary indicates current `FEATURE` table in `CONFIG_DB`.
            expected_table A dictionary indicates the expected `FEATURE` table in `CONFIG_DB`.

        Returns:
            Returns True if `FEATURE` table in `CONFIG_DB` was not modified unexpectedly;
            otherwise, returns False.
        """
        ddiff = DeepDiff(feature_table, expected_table, ignore_order=True)

        return True if not ddiff else False

    def checks_systemd_config_file(self, feature_table, feature_systemd_name_map=None):
        """Checks whether the systemd configuration file of each feature was created or not
        and whether the `Restart=` field in the file is set correctly or not.

        Args:
            feature_table: A dictionary indicates `Feature` table in `CONFIG_DB`.

        Returns: Boolean value indicates whether test passed or not.
        """

        truth_table = {'enabled': 'always',
                       'disabled': 'no'}

        systemd_config_file_path = os.path.join(hostcfgd.FeatureHandler.SYSTEMD_SERVICE_CONF_DIR,
                                                'auto_restart.conf')

        for feature_name in feature_table:
            auto_restart_status = feature_table[feature_name].get('auto_restart', 'disabled')
            if "enabled" in auto_restart_status:
                auto_restart_status = "enabled"
            elif "disabled" in auto_restart_status:
                auto_restart_status = "disabled"

            feature_systemd_list = feature_systemd_name_map[feature_name] if feature_systemd_name_map else [feature_name]

            for feature_systemd in feature_systemd_list:
                feature_systemd_config_file_path = systemd_config_file_path.format(feature_systemd)
                is_config_file_existing = os.path.exists(feature_systemd_config_file_path)
                assert is_config_file_existing, "Systemd configuration file of feature '{}' does not exist!".format(feature_systemd)

                with open(feature_systemd_config_file_path) as systemd_config_file:
                    status = systemd_config_file.read().strip()
                assert status == '[Service]\nRestart={}'.format(truth_table[auto_restart_status])

    def get_state_db_set_calls(self, feature_table):
        """Returns a Mock call objects which recorded the `set` calls to `FEATURE` table in `STATE_DB`.

        Args:
            feature_table: A dictionary indicates `FEATURE` table in `CONFIG_DB`.

        Returns:
            set_call_list: A list indicates Mock call objects.
        """
        set_call_list = []

        for feature_name in feature_table.keys():
            feature_state = ""
            if "enabled" in feature_table[feature_name]["state"]:
                feature_state = "enabled"
            elif "disabled" in feature_table[feature_name]["state"]:
                feature_state = "disabled"
            else:
                feature_state = feature_table[feature_name]["state"]

            set_call_list.append(mock.call(feature_name, [("state", feature_state)]))

        return set_call_list

    @parameterized.expand(HOSTCFGD_TEST_VECTOR)
    @patchfs
    def test_sync_state_field(self, test_scenario_name, config_data, fs):
        """Tests the method `sync_state_field(...)` of `FeatureHandler` class.

        Args:
            test_secnario_name: A string indicates different testing scenario.
            config_data: A dictionary contains initial `CONFIG_DB` tables and expected results.

        Returns:
            Boolean value indicates whether test will pass or not.
        """
        # add real path of sesscommon for database_config.json
        fs.add_real_paths(swsscommon_package.__path__)
        fs.create_dir(hostcfgd.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            with mock.patch("sonic_py_common.device_info.get_device_runtime_metadata", return_value=config_data['device_runtime_metadata']):
                with mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=True if 'num_npu' in config_data else False):
                    with mock.patch("sonic_py_common.device_info.get_num_npus", return_value=config_data['num_npu'] if 'num_npu' in config_data else 1):
                        with mock.patch("sonic_py_common.device_info.get_namespaces", return_value=["asic{}".format(a) for a in  range(config_data['num_npu'])] if 'num_npu' in config_data else []):
                            popen_mock = mock.Mock()
                            attrs = config_data['popen_attributes']
                            popen_mock.configure_mock(**attrs)
                            mocked_subprocess.Popen.return_value = popen_mock

                            device_config = {}
                            device_config['DEVICE_METADATA'] = MockConfigDb.CONFIG_DB['DEVICE_METADATA']
                            device_config.update(config_data['device_runtime_metadata'])

                            feature_handler = hostcfgd.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_config)
                            feature_table = MockConfigDb.CONFIG_DB['FEATURE']
                            feature_handler.sync_state_field(feature_table)

                            feature_systemd_name_map = {}
                            for feature_name in feature_table.keys():
                                feature = hostcfgd.Feature(feature_name, feature_table[feature_name], device_config)
                                feature_names, _ = feature_handler.get_multiasic_feature_instances(feature)
                                feature_systemd_name_map[feature_name] = feature_names

                            is_any_difference = self.checks_config_table(MockConfigDb.get_config_db()['FEATURE'],
                                                                         config_data['expected_config_db']['FEATURE'])
                            assert is_any_difference, "'FEATURE' table in 'CONFIG_DB' is modified unexpectedly!"

                            if 'num_npu' in config_data:
                                for ns in range(config_data['num_npu']):
                                    namespace = "asic{}".format(ns)
                                    is_any_difference = self.checks_config_table(feature_handler.ns_cfg_db[namespace].get_config_db()['FEATURE'],
                                                                                 config_data['expected_config_db']['FEATURE'])
                                    assert is_any_difference, "'FEATURE' table in 'CONFIG_DB' in namespace {} is modified unexpectedly!".format(namespace)

                            feature_table_state_db_calls = self.get_state_db_set_calls(feature_table)

                            self.checks_systemd_config_file(config_data['config_db']['FEATURE'], feature_systemd_name_map)
                            mocked_subprocess.check_call.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                          any_order=True)
                            mocked_subprocess.check_call.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                          any_order=True)
                            feature_state_table_mock.set.assert_has_calls(feature_table_state_db_calls)
                            self.checks_systemd_config_file(config_data['config_db']['FEATURE'], feature_systemd_name_map)

    @parameterized.expand(HOSTCFGD_TEST_VECTOR)
    @patchfs
    def test_handler(self, test_scenario_name, config_data, fs):
        """Tests the method `handle(...)` of `FeatureHandler` class.

        Args:
            test_secnario_name: A string indicates different testing scenario.
            config_data: A dictionary contains initial `CONFIG_DB` tables and expected results.

        Returns:
            Boolean value indicates whether test will pass or not.
        """
        # add real path of sesscommon for database_config.json
        fs.add_real_paths(swsscommon_package.__path__)
        fs.create_dir(hostcfgd.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            with mock.patch("sonic_py_common.device_info.get_device_runtime_metadata", return_value=config_data['device_runtime_metadata']):
                with mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=True if 'num_npu' in config_data else False):
                    with mock.patch("sonic_py_common.device_info.get_num_npus", return_value=config_data['num_npu'] if 'num_npu' in config_data else 1):
                        popen_mock = mock.Mock()
                        attrs = config_data['popen_attributes']
                        popen_mock.configure_mock(**attrs)
                        mocked_subprocess.Popen.return_value = popen_mock

                        device_config = {}
                        device_config['DEVICE_METADATA'] = MockConfigDb.CONFIG_DB['DEVICE_METADATA']
                        device_config.update(config_data['device_runtime_metadata'])
                        feature_handler = hostcfgd.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_config)

                        feature_table = MockConfigDb.CONFIG_DB['FEATURE']

                        feature_systemd_name_map = {}
                        for feature_name, feature_config in feature_table.items():
                            feature_handler.handler(feature_name, 'SET', feature_config)
                            feature = hostcfgd.Feature(feature_name, feature_table[feature_name], device_config)
                            feature_names, _ = feature_handler.get_multiasic_feature_instances(feature)
                            feature_systemd_name_map[feature_name] = feature_names

                        self.checks_systemd_config_file(config_data['config_db']['FEATURE'], feature_systemd_name_map)
                        mocked_subprocess.check_call.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                      any_order=True)
                        mocked_subprocess.check_call.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                      any_order=True)

    def test_feature_config_parsing(self):
        swss_feature = hostcfgd.Feature('swss', {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'has_timer': 'True',
            'has_global_scope': 'False',
            'has_per_asic_scope': 'True',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'enabled'
        assert swss_feature.has_timer
        assert not swss_feature.has_global_scope
        assert swss_feature.has_per_asic_scope

    def test_feature_config_parsing_defaults(self):
        swss_feature = hostcfgd.Feature('swss', {
            'state': 'enabled',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'disabled'
        assert not swss_feature.has_timer
        assert swss_feature.has_global_scope
        assert not swss_feature.has_per_asic_scope


class TesNtpCfgd(TestCase):
    """
        Test hostcfd daemon - NtpCfgd
    """
    def setUp(self):
        MockConfigDb.CONFIG_DB['NTP'] = {'global': {'vrf': 'mgmt', 'src_intf': 'eth0'}}
        MockConfigDb.CONFIG_DB['NTP_SERVER'] = {'0.debian.pool.ntp.org': {}}

    def tearDown(self):
        MockConfigDb.CONFIG_DB = {}

    def test_ntp_global_update_with_no_servers(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.ntp_global_update('global', MockConfigDb.CONFIG_DB['NTP']['global'])

            mocked_subprocess.check_call.assert_not_called()

    def test_ntp_global_update_ntp_servers(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.ntp_global_update('global', MockConfigDb.CONFIG_DB['NTP']['global'])
            ntpcfgd.ntp_server_update('0.debian.pool.ntp.org', 'SET')
            mocked_subprocess.check_call.assert_has_calls([call(['systemctl', 'restart', 'ntp-config'])])

    def test_loopback_update(self):
        with mock.patch('hostcfgd.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock

            ntpcfgd = hostcfgd.NtpCfg()
            ntpcfgd.ntp_global = MockConfigDb.CONFIG_DB['NTP']['global']
            ntpcfgd.ntp_servers.add('0.debian.pool.ntp.org')

            ntpcfgd.handle_ntp_source_intf_chg('eth0')
            mocked_subprocess.check_call.assert_has_calls([call(['systemctl', 'restart', 'ntp-config'])])


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

    @patchfs
    def test_feature_events(self, fs):
        fs.create_dir(hostcfgd.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        MockConfigDb.event_queue = [('FEATURE', 'dhcp_relay'),
                                ('FEATURE', 'mux'),
                                ('FEATURE', 'telemetry')]
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
            expected = [call(['sudo', 'systemctl', 'daemon-reload']),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service']),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service']),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service']),
                        call(['sudo', 'systemctl', 'daemon-reload']),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service']),
                        call(['sudo', 'systemctl', 'enable', 'mux.service']),
                        call(['sudo', 'systemctl', 'start', 'mux.service']),
                        call(['sudo', 'systemctl', 'daemon-reload']),
                        call(['sudo', 'systemctl', 'unmask', 'telemetry.service']),
                        call(['sudo', 'systemctl', 'unmask', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'enable', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'start', 'telemetry.timer'])]
            mocked_subprocess.check_call.assert_has_calls(expected)

            # Change the state to disabled
            MockConfigDb.CONFIG_DB['FEATURE']['telemetry']['state'] = 'disabled'
            MockConfigDb.event_queue = [('FEATURE', 'telemetry')]
            try:
                daemon.start()
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'stop', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'disable', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'mask', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'stop', 'telemetry.service']),
                        call(['sudo', 'systemctl', 'disable', 'telemetry.timer']),
                        call(['sudo', 'systemctl', 'mask', 'telemetry.timer'])]
            mocked_subprocess.check_call.assert_has_calls(expected)

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
            expected = [call(['systemctl', 'restart', 'ntp-config']),
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
            expected = [call(['sonic-kdump-config', '--disable']),
                        call(['sonic-kdump-config', '--num_dumps', '3']),
                        call(['sonic-kdump-config', '--memory', '0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M'])]
            mocked_subprocess.check_call.assert_has_calls(expected, any_order=True)

    def test_devicemeta_event(self):
        """
        Test handling DEVICE_METADATA events.
        1) Hostname reload
        1) Timezone reload
        """
        MockConfigDb.set_config_db(HOSTCFG_DAEMON_CFG_DB)
        MockConfigDb.event_queue = [(swsscommon.CFG_DEVICE_METADATA_TABLE_NAME,
                                    'localhost')]
        daemon = hostcfgd.HostConfigDaemon()
        daemon.aaacfg = mock.MagicMock()
        daemon.iptables = mock.MagicMock()
        daemon.passwcfg = mock.MagicMock()
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
                call(['timedatectl', 'set-timezone', 'Europe/Kyiv'])
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
                    call(['sudo', 'systemctl', 'restart', 'ntp-config']),
                    call(['service', 'ntp', 'stop']),
                    call(['systemctl', 'restart', 'interfaces-config']),
                    call(['service', 'ntp', 'start']),
                    call(['ip', '-4', 'route', 'del', 'default', 'dev', 'eth0', 'metric', '202'])
                ]
                mocked_subprocess.check_call.assert_has_calls(expected)
                expected = [
                    call(['cat', '/proc/net/route'], ['grep', '-E', r"eth0\s+00000000\s+[0-9A-Z]+\s+[0-9]+\s+[0-9]+\s+[0-9]+\s+202"], ['wc', '-l'])
                ]
                mocked_check_output.assert_has_calls(expected)

class TestSyslogHandler:
    @mock.patch('hostcfgd.run_cmd')
    @mock.patch('hostcfgd.SyslogCfg.parse_syslog_conf', mock.MagicMock(return_value=('100', '200')))
    def test_syslog_update(self, mock_run_cmd):
        syslog_cfg = hostcfgd.SyslogCfg()
        data = {
            'rate_limit_interval': '100',
            'rate_limit_burst': '200'
        }
        syslog_cfg.syslog_update(data)
        mock_run_cmd.assert_not_called()

        data = {
            'rate_limit_interval': '200',
            'rate_limit_burst': '200'
        }
        syslog_cfg.syslog_update(data)
        expected = [call(['systemctl', 'reset-failed', 'rsyslog-config', 'rsyslog'], raise_exception=True),
                    call(['systemctl', 'restart', 'rsyslog-config'], raise_exception=True)]
        mock_run_cmd.assert_has_calls(expected)

        data = {
            'rate_limit_interval': '100',
            'rate_limit_burst': '100'
        }
        mock_run_cmd.side_effect = Exception()
        syslog_cfg.syslog_update(data)
        # when exception occurs, interval and burst should not be updated
        assert syslog_cfg.current_interval == '200'
        assert syslog_cfg.current_burst == '200'

    def test_load(self):
        syslog_cfg = hostcfgd.SyslogCfg()
        syslog_cfg.syslog_update = mock.MagicMock()

        data = {}
        syslog_cfg.load(data)
        syslog_cfg.syslog_update.assert_not_called()

        data = {syslog_cfg.HOST_KEY: {}}
        syslog_cfg.load(data)
        syslog_cfg.syslog_update.assert_called_once()

    def test_parse_syslog_conf(self):
        syslog_cfg = hostcfgd.SyslogCfg()

        syslog_cfg.SYSLOG_CONF_PATH = os.path.join(test_path, 'hostcfgd', 'mock_rsyslog.conf')
        interval, burst = syslog_cfg.parse_syslog_conf()
        assert interval == '50'
        assert burst == '10002'

        syslog_cfg.SYSLOG_CONF_PATH = os.path.join(test_path, 'hostcfgd', 'mock_empty_rsyslog.conf')
        interval, burst = syslog_cfg.parse_syslog_conf()
        assert interval == '0'
        assert burst == '0'
