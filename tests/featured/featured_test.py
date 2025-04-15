import os
import sys
import time
import copy
import swsscommon as swsscommon_package
from sonic_py_common import device_info
from swsscommon import swsscommon

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock

from .test_vectors import FEATURED_TEST_VECTOR, FEATURE_DAEMON_CFG_DB
from tests.common.mock_configdb import MockConfigDb, MockDBConnector, MockSubscriberStateTable, MockSelect
from tests.common.mock_restart_waiter import MockRestartWaiter

from pyfakefs.fake_filesystem_unittest import patchfs, Patcher
from deepdiff import DeepDiff
from unittest.mock import call

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

# Load the file under test
featured_path = os.path.join(scripts_path, 'featured')
featured = load_module_from_source('featured', featured_path)
featured.ConfigDBConnector = MockConfigDb
featured.DBConnector = MockDBConnector
featured.Table = mock.Mock()
swsscommon.Select = MockSelect
swsscommon.SubscriberStateTable = MockSubscriberStateTable
swsscommon.RestartWaiter = MockRestartWaiter

def syslog_side_effect(pri, msg): 
    print(f"{pri}: {msg}")

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

    def checks_systemd_config_file(self, device_type, feature_table, feature_systemd_name_map=None):
        """Checks whether the systemd configuration file of each feature was created or not
        and whether the `Restart=` field in the file is set correctly or not.

        Args:
            feature_table: A dictionary indicates `Feature` table in `CONFIG_DB`.

        Returns: Boolean value indicates whether test passed or not.
        """

        truth_table = {'enabled': 'always',
                       'disabled': 'no'}

        systemd_config_file_path = os.path.join(featured.FeatureHandler.SYSTEMD_SERVICE_CONF_DIR,
                                                'auto_restart.conf')

        for feature_name in feature_table:
            is_dependent_feature = True if feature_name in ['syncd', 'gbsyncd'] else False
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
                    if device_type == 'SpineRouter' and is_dependent_feature:
                        assert status == '[Service]\nRestart=no'
                    else:
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

    @parameterized.expand(FEATURED_TEST_VECTOR)
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
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('featured.subprocess') as mocked_subprocess:
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
                            device_type = MockConfigDb.CONFIG_DB['DEVICE_METADATA']['localhost']['type']

                            feature_handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock,
                                                                      device_config, False)
                            feature_handler.is_delayed_enabled = True
                            feature_table = MockConfigDb.CONFIG_DB['FEATURE']
                            feature_handler.sync_state_field(feature_table)

                            feature_systemd_name_map = {}
                            for feature_name in feature_table.keys():
                                feature = featured.Feature(feature_name, feature_table[feature_name], device_config)
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

                            self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)
                            mocked_subprocess.run.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                          any_order=True)
                            mocked_subprocess.run.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                          any_order=True)
                            feature_state_table_mock.set.assert_has_calls(feature_table_state_db_calls)
                            self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)

    @parameterized.expand(FEATURED_TEST_VECTOR)
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
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('featured.subprocess') as mocked_subprocess:
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
                        device_type = MockConfigDb.CONFIG_DB['DEVICE_METADATA']['localhost']['type']
                        feature_handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock,
                                                                  device_config, False)
                        feature_handler.is_delayed_enabled = True

                        feature_table = MockConfigDb.CONFIG_DB['FEATURE']

                        feature_systemd_name_map = {}
                        for feature_name, feature_config in feature_table.items():
                            feature_handler.handler(feature_name, 'SET', feature_config)
                            feature = featured.Feature(feature_name, feature_table[feature_name], device_config)
                            feature_names, _ = feature_handler.get_multiasic_feature_instances(feature)
                            feature_systemd_name_map[feature_name] = feature_names

                        self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)
                        mocked_subprocess.run.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                      any_order=True)
                        mocked_subprocess.run.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                      any_order=True)

    def test_feature_config_parsing(self):
        swss_feature = featured.Feature('swss', {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'delayed': 'True',
            'has_global_scope': 'False',
            'has_per_asic_scope': 'True',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'enabled'
        assert swss_feature.delayed
        assert not swss_feature.has_global_scope
        assert swss_feature.has_per_asic_scope

    def test_feature_config_parsing_defaults(self):
        swss_feature = featured.Feature('swss', {
            'state': 'enabled',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'disabled'
        assert not swss_feature.delayed
        assert swss_feature.has_global_scope
        assert not swss_feature.has_per_asic_scope
    
    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_scope', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    def test_feature_resync(self):
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_table = {
            'sflow': {
                'state': 'enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        mock_db.get_entry.return_value = None
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_called_with('FEATURE', 'sflow', {'state': 'enabled'})
        mock_db.mod_entry.reset_mock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'state': 'disabled',
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_not_called()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_table = {
            'sflow': {
                'state': 'always_enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_called_with('FEATURE', 'sflow', {'state': 'always_enabled'})
        mock_db.mod_entry.reset_mock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'state': 'some template',
        }
        feature_table = {
            'sflow': {
                'state': 'enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_called_with('FEATURE', 'sflow', {'state': 'enabled'})
    
    def test_port_init_done_twice(self):
        """There could be multiple "PortInitDone" event in case of swss
        restart(either due to crash or due to manual operation). swss
        restarting would cause all services that depend on it to be stopped.
        Those stopped services which have delayed=True will not be auto
        restarted by systemd, featured is responsible for enabling those services
        when swss is ready. This test case covers it.
        """
        feature_handler = featured.FeatureHandler(None, None, {}, False)
        assert not feature_handler.is_delayed_enabled
        feature_handler.port_listener(key='PortInitDone', op='SET', data=None)
        assert feature_handler.is_delayed_enabled
        
        feature_handler.enable_delayed_services = mock.MagicMock()
        feature_handler.port_listener(key='PortInitDone', op='SET', data=None)
        feature_handler.enable_delayed_services.assert_called_once()


@mock.patch("syslog.syslog", side_effect=syslog_side_effect)
@mock.patch('sonic_py_common.device_info.get_device_runtime_metadata')
class TestFeatureDaemon(TestCase):

    def setUp(self):
        print("Running Setup")
        self.patcher = Patcher()
        self.patcher.setUp()
        self.patcher.fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        MockConfigDb.CONFIG_DB = copy.deepcopy(FEATURE_DAEMON_CFG_DB)
        MockRestartWaiter.advancedReboot = False
        MockSelect.NUM_TIMEOUT_TRIES = 0

    def tearDown(self):
        print("Running TearDown")
        self.patcher.tearDown()
        MockConfigDb.CONFIG_DB.clear()
        MockSelect.reset_event_queue()

    def test_feature_events(self, mock_syslog, get_runtime):
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux'),
                                    ('FEATURE', 'telemetry')])
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:
                daemon.start(time.time())
            except TimeoutError as e:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

            # Change the state to disabled
            MockSelect.reset_event_queue()
            MockConfigDb.CONFIG_DB['FEATURE']['dhcp_relay']['state'] = 'disabled'
            MockSelect.set_event_queue([('FEATURE', 'dhcp_relay')])
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'stop', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'disable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'mask', 'dhcp_relay.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_delayed_service(self, mock_syslog, get_runtime):
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux'),
                                    ('FEATURE', 'telemetry'),
                                    ('PORT_TABLE', 'PortInitDone')])
        # Note: To simplify testing, subscriberstatetable only read from CONFIG_DB
        MockConfigDb.CONFIG_DB['PORT_TABLE'] = {'PortInitDone': {'lanes': '0'}, 'PortConfigDone': {'val': 'true'}}
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.register_callbacks()
            daemon.render_all_feature_states()
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'telemetry.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'telemetry.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'telemetry.service'], capture_output=True, check=True, text=True)]

            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_advanced_reboot(self, mock_syslog, get_runtime):
        MockRestartWaiter.advancedReboot = True
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:            
                daemon.start(time.time())
            except TimeoutError:
                pass        
            expected = [
                call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'telemetry.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'telemetry.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'telemetry.service'], capture_output=True, check=True, text=True)]               
        
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_portinit_timeout(self, mock_syslog, get_runtime):
        print(MockConfigDb.CONFIG_DB)
        MockSelect.NUM_TIMEOUT_TRIES = 1
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux'),
                                    ('FEATURE', 'telemetry')])
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:
                daemon.start(0.0)
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'telemetry.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'telemetry.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'telemetry.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_systemctl_command_failure(self, mock_syslog, get_runtime):
        """Test that when systemctl commands fail:
        1. The feature state is not cached
        2. The feature state is set to FAILED
        3. The update_feature_state returns False
        """
        mock_db = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_handler.is_delayed_enabled = True

        # Create a feature that should be enabled
        feature_name = 'test_feature'
        feature_cfg = {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'delayed': 'False',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False'
        }

        # Initialize the feature in cached_config using the same pattern as in featured
        feature = featured.Feature(feature_name, feature_cfg)
        feature_handler._cached_config.setdefault(feature_name, featured.Feature(feature_name, {}))

        # Mock subprocess.run and Popen to simulate command failure
        with mock.patch('featured.subprocess') as mocked_subprocess:
            # Mock Popen for get_systemd_unit_state
            popen_mock = mock.Mock()
            popen_mock.communicate.return_value = ('enabled', '')
            popen_mock.returncode = 1
            mocked_subprocess.Popen.return_value = popen_mock

            # Mock run_cmd to raise an exception
            with mock.patch('featured.run_cmd') as mocked_run_cmd:
                mocked_run_cmd.side_effect = Exception("Command failed")

                # Try to update feature state
                result = feature_handler.update_feature_state(feature)

                # Verify the result is False
                assert result is False

                # Verify the feature state was set to FAILED
                mock_feature_state_table.set.assert_called_with('test_feature', [('state', 'failed')])

                # Verify the feature state was not enabled in the cache
                assert feature_handler._cached_config[feature.name].state != 'enabled'
