import os
import subprocess
import sys
import time
import copy
import socket
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

class TestRunCmd(TestCase):
    """Tests for command failure logging."""

    @mock.patch("featured.syslog.syslog")
    def test_nonzero_exit_logs_stdout_and_stderr(self, mock_syslog):
        cmd = ["systemctl", "stop", "teamd.service"]
        command_error = subprocess.CalledProcessError(returncode=1, cmd=cmd, output="", stderr="stop failed")

        with mock.patch("featured.subprocess.run", side_effect=command_error):
            featured.run_cmd(cmd)

        log_message = mock_syslog.call_args.args[1]
        assert str(cmd) in log_message
        assert "return code - 1" in log_message
        assert "stdout:\n" in log_message
        assert "stderr:\nstop failed" in log_message

    @mock.patch("featured.syslog.syslog")
    def test_unexpected_error_logging_preserves_original_exception(self, mock_syslog):
        cmd = ["missing-systemctl", "stop", "teamd.service"]
        command_error = FileNotFoundError(2, "No such file or directory", cmd[0])

        with mock.patch("featured.subprocess.run", side_effect=command_error):
            with self.assertRaises(FileNotFoundError) as raised_error:
                featured.run_cmd(cmd, raise_exception=True)

        assert raised_error.exception is command_error
        log_message = mock_syslog.call_args.args[1]
        assert str(cmd) in log_message
        assert str(command_error) in log_message


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
        # Missing FEATURE row: resync_feature_state skips mod_entry (avoid recreating a row removed concurrently).
        mock_db.mod_entry.assert_not_called()
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
        mock_db.mod_entry.reset_mock()

        # Partial entry (no 'state' key): left behind by sync_feature_scope racing with
        # deregister.  resync_feature_state must delete it rather than completing it.
        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }
        mock_db.set_entry = mock.MagicMock()
        feature_handler.sync_state_field(feature_table)
        mock_db.set_entry.assert_called_with('FEATURE', 'sflow', None)
        mock_db.mod_entry.assert_not_called()

    def test_sync_feature_scope_toctou(self):
        """sync_feature_scope must delete a partial entry it accidentally creates when
        the FEATURE row is deleted by a concurrent deregister between the mod_entry and
        the post-write existence check (TOCTOU)."""
        mock_db = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature = featured.Feature('sflow', {
            'state': 'disabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False',
            'delayed': 'False',
        })

        # Post-write check sees a partial entry (no 'state' key): the FEATURE row was
        # deleted by deregister and then recreated by mod_entry during the race
        # window, leaving only the scope fields.
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }

        feature_handler.sync_feature_scope(feature)
        mock_db.set_entry.assert_called_with(featured.FEATURE_TBL, 'sflow', None)

    def test_sync_state_field_two_phase_ordering(self):
        """Verify all systemd configs are written (Phase 1) before any service
        is started (Phase 2), with exactly one daemon-reload in between."""
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock(return_value=None)
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_handler.is_delayed_enabled = True

        call_order = []

        def track_update_systemd(feature, reload=True):
            call_order.append(('update_systemd_config', feature.name, reload))

        def track_reload():
            call_order.append(('reload_systemd_config',))

        def track_update_feature_state(feature):
            call_order.append(('update_feature_state', feature.name))
            return True

        feature_table = {
            'sflow': {'state': 'enabled', 'auto_restart': 'enabled'},
            'snmp': {'state': 'enabled', 'auto_restart': 'enabled'},
            'lldp': {'state': 'enabled', 'auto_restart': 'disabled'},
        }

        with mock.patch.object(feature_handler, 'update_systemd_config', side_effect=track_update_systemd), \
             mock.patch.object(feature_handler, 'reload_systemd_config', side_effect=track_reload), \
             mock.patch.object(feature_handler, 'update_feature_state', side_effect=track_update_feature_state), \
             mock.patch.object(feature_handler, 'sync_feature_scope'), \
             mock.patch.object(feature_handler, 'resync_feature_state'), \
             mock.patch.object(feature_handler, 'sync_feature_delay_state'):
            feature_handler.sync_state_field(feature_table)

        systemd_calls = [c for c in call_order if c[0] == 'update_systemd_config']
        assert len(systemd_calls) == 3
        for c in systemd_calls:
            assert c[2] is False, "update_systemd_config for {} should use reload=False".format(c[1])

        reload_calls = [c for c in call_order if c[0] == 'reload_systemd_config']
        assert len(reload_calls) == 1

        last_config_idx = max(i for i, c in enumerate(call_order) if c[0] == 'update_systemd_config')
        reload_idx = next(i for i, c in enumerate(call_order) if c[0] == 'reload_systemd_config')
        state_indices = [i for i, c in enumerate(call_order) if c[0] == 'update_feature_state']
        assert last_config_idx < reload_idx, "All config writes must complete before daemon-reload"
        assert all(idx > reload_idx for idx in state_indices), "All service starts must happen after daemon-reload"

    def test_update_systemd_config_reload_parameter(self):
        """Verify update_systemd_config only triggers daemon-reload when reload=True (default)."""
        mock_db = mock.MagicMock()
        feature_handler = featured.FeatureHandler(mock_db, mock.MagicMock(), {}, False)
        feature = featured.Feature('sflow', {'state': 'enabled', 'auto_restart': 'enabled'})

        with mock.patch.object(feature_handler, 'reload_systemd_config') as mock_reload, \
             mock.patch.object(feature_handler, 'get_multiasic_feature_instances',
                               return_value=(['sflow'], ['service'])), \
             mock.patch('os.path.exists', return_value=True), \
             mock.patch('builtins.open', mock.mock_open()):
            feature_handler.update_systemd_config(feature, reload=False)
            mock_reload.assert_not_called()

            feature_handler.update_systemd_config(feature)
            mock_reload.assert_called_once()

    def test_update_systemd_config_logs_requested_and_written_restart(self):
        """Verify the log shows how auto_restart maps to the written systemd value."""
        test_cases = [
            ('teamd', 'FixedSwitch', 'enabled', 'always'),
            ('syncd', 'SpineRouter', 'enabled', 'no'),
        ]

        for feature_name, device_type, auto_restart, written_restart in test_cases:
            with self.subTest(feature_name=feature_name, device_type=device_type):
                device_config = {'DEVICE_METADATA': {'localhost': {'type': device_type}}}
                feature_handler = featured.FeatureHandler(mock.MagicMock(), mock.MagicMock(),
                                                          device_config, False)
                feature = featured.Feature(feature_name, {
                    'state': 'enabled',
                    'auto_restart': auto_restart,
                })
                expected_log = (f"Updated auto-restart config for {feature_name}.service: "
                                f"auto_restart={auto_restart} -> Restart={written_restart}")

                with mock.patch.object(feature_handler, 'get_multiasic_feature_instances',
                                       return_value=([feature_name], ['service'])), \
                     mock.patch('featured.os.path.exists', return_value=True), \
                     mock.patch('builtins.open', mock.mock_open()), \
                     mock.patch('featured.syslog.syslog') as mock_syslog:
                    feature_handler.update_systemd_config(feature, reload=False)

                mock_syslog.assert_any_call(featured.syslog.LOG_INFO, expected_log)

    def test_sync_state_field_empty_table(self):
        """With no features, daemon-reload still fires and no services are started."""
        mock_db = mock.MagicMock()
        feature_handler = featured.FeatureHandler(mock_db, mock.MagicMock(), {}, False)

        with mock.patch.object(feature_handler, 'update_systemd_config') as mock_config, \
             mock.patch.object(feature_handler, 'reload_systemd_config') as mock_reload, \
             mock.patch.object(feature_handler, 'update_feature_state') as mock_state:
            feature_handler.sync_state_field({})

        mock_config.assert_not_called()
        mock_reload.assert_called_once()
        mock_state.assert_not_called()

    @mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=False)
    def test_port_init_done_twice(self, mock_is_multi_npu):
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

    @staticmethod
    def _make_multi_asic_handler(quorum):
        """Multi-ASIC FeatureHandler built without __init__ (no live DB connections)."""
        fh = featured.FeatureHandler.__new__(featured.FeatureHandler)
        fh.is_multi_npu = True
        fh.is_delayed_enabled = False
        fh._cached_config = {}
        fh.port_init_quorum_ns = set(quorum)
        fh.pending_port_init_ns = set(quorum)
        return fh

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_quorum_multi_asic(self, mock_syslog):
        """Multi-ASIC releases only after every namespace reported PortInitDone."""
        fh = self._make_multi_asic_handler(['asic0', 'asic1', 'asic2', 'asic3'])
        fh.enable_delayed_services = mock.MagicMock()
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic2')
        fh.enable_delayed_services.assert_not_called()   # quorum not complete yet
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic3')
        fh.enable_delayed_services.assert_called_once()   # all ASICs reported

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_requorum_after_swss_restart_multi_asic(self, mock_syslog):
        """After the first release, DEL then SET on one ASIC (swss restart) re-enables on its own."""
        fh = self._make_multi_asic_handler(['asic0', 'asic1'])
        fh.enable_delayed_services = mock.MagicMock(side_effect=lambda: setattr(fh, 'is_delayed_enabled', True))
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')   # release #1
        fh.port_listener('PortInitDone', 'DEL', None, namespace='asic1')   # swss@1 stop
        self.assertEqual(fh.pending_port_init_ns, {'asic1'})
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')   # swss@1 back
        self.assertEqual(fh.enable_delayed_services.call_count, 2)
        self.assertEqual(fh.pending_port_init_ns, set())

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_withdrawn_marker_blocks_release(self, mock_syslog):
        """A marker withdrawn before the first release counts as not ready until republished."""
        fh = self._make_multi_asic_handler(['asic0', 'asic1', 'asic2'])
        fh.enable_delayed_services = mock.MagicMock()
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'DEL', None, namespace='asic0')   # asic0 swss restarts
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic2')
        fh.enable_delayed_services.assert_not_called()                     # asic0 not ready
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.enable_delayed_services.assert_called_once()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_overlapping_swss_restarts(self, mock_syslog):
        """After the first release an ASIC that never returns cannot hold up the others."""
        fh = self._make_multi_asic_handler(['asic0', 'asic1'])
        fh.enable_delayed_services = mock.MagicMock(side_effect=lambda: setattr(fh, 'is_delayed_enabled', True))
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')   # release #1
        fh.port_listener('PortInitDone', 'DEL', None, namespace='asic0')   # asic0 down for good
        fh.port_listener('PortInitDone', 'DEL', None, namespace='asic1')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')   # asic1 back
        self.assertEqual(fh.enable_delayed_services.call_count, 2)
        self.assertEqual(fh.pending_port_init_ns, {'asic0'})
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')   # replay/no-op: not pending
        self.assertEqual(fh.enable_delayed_services.call_count, 2)

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_extra_namespace_does_not_release_early(self, mock_syslog):
        """A PortInitDone from a namespace outside the quorum must not release early."""
        fh = self._make_multi_asic_handler(['asic0', 'asic1'])
        fh.enable_delayed_services = mock.MagicMock()
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic9')   # not in quorum
        fh.enable_delayed_services.assert_not_called()
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic1')
        fh.enable_delayed_services.assert_called_once()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_port_init_ignores_other_keys_and_ops(self, mock_syslog):
        """Only PortInitDone SET/DEL matter; other keys and ops leave the quorum alone."""
        fh = self._make_multi_asic_handler(['asic0'])
        fh.enable_delayed_services = mock.MagicMock()
        fh.port_listener('Ethernet0', 'SET', None, namespace='asic0')
        fh.port_listener('Ethernet0', 'DEL', None, namespace='asic0')
        fh.port_listener('PortInitDone', 'HSET', None, namespace='asic0')
        fh.port_listener('', 'SET', None, namespace='asic0')
        fh.enable_delayed_services.assert_not_called()
        self.assertEqual(fh.pending_port_init_ns, {'asic0'})
        fh.port_listener('PortInitDone', 'SET', None, namespace='asic0')
        fh.enable_delayed_services.assert_called_once()

    @mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=False)
    def test_port_init_done_del_ignored_single_asic(self, mock_is_multi_npu):
        """Single-ASIC keeps the stateless behavior: a DEL never enables anything."""
        feature_handler = featured.FeatureHandler(None, None, {}, False)
        feature_handler.enable_delayed_services = mock.MagicMock()
        feature_handler.port_listener(key='PortInitDone', op='DEL', data=None)
        feature_handler.enable_delayed_services.assert_not_called()
        feature_handler.port_listener(key='PortInitDone', op='SET', data=None)
        feature_handler.enable_delayed_services.assert_called_once()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_enable_and_disable_feature_ExclusionList_skips_actions(self, mock_syslog):
        """Verify that ExclusionList feature 'frr_bmp' is skipped in both enable and disable."""
        feature_state_table_mock = mock.Mock()
        device_cfg = {"DEVICE_METADATA": {"localhost": {"type": "FixedSwitch"}}}
        handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_cfg, False)

        feat_cfg = {"state": "enabled", "auto_restart": "enabled"}
        feature = featured.Feature("frr_bmp", feat_cfg, device_cfg)

        with mock.patch.object(handler, "get_multiasic_feature_instances",
                            return_value=(["frr_bmp"], ["service"])), \
            mock.patch.object(handler, "get_systemd_unit_state", return_value="disabled"), \
            mock.patch("featured.run_cmd") as mocked_run_cmd, \
            mock.patch.object(handler, "set_feature_state") as mocked_set_state:

            # --- enable_feature() ---
            handler.enable_feature(feature)

            mocked_run_cmd.assert_not_called()
            mocked_set_state.assert_not_called()
            assert any("ExclusionList: skip enabling 'frr_bmp'" in str(c.args[1]) for c in mock_syslog.call_args_list)

        mock_syslog.reset_mock()
        with mock.patch.object(handler, "get_multiasic_feature_instances",
                            return_value=(["frr_bmp"], ["service"])), \
            mock.patch.object(handler, "get_systemd_unit_state", return_value="enabled"), \
            mock.patch("featured.run_cmd") as mocked_run_cmd, \
            mock.patch.object(handler, "set_feature_state") as mocked_set_state:

            # --- disable_feature() ---
            handler.disable_feature(feature)

            mocked_run_cmd.assert_not_called()
            mocked_set_state.assert_not_called()
            assert any("ExclusionList: skip disabling 'frr_bmp'" in str(c.args[1]) for c in mock_syslog.call_args_list)

    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.get_systemd_unit_state', mock.MagicMock(return_value=""))
    def test_sync_feature_scope_conditional_write(self):
        """Verify sync_feature_scope only writes when scope values differ or entry is missing."""
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)

        feature = featured.Feature('sflow', {
            'state': 'enabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False',
        })

        # Values already match -> no write
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_not_called()
        mock_db.mod_entry.reset_mock()

        # Only has_per_asic_scope differs -> write only changed field in single call
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'True',
            'has_global_scope': 'True',
        }
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {'has_per_asic_scope': 'False'})
        mock_db.mod_entry.reset_mock()

        # Entry missing (None) -> write both fields in single call
        mock_db.get_entry.return_value = None
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        })

    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.get_systemd_unit_state', mock.MagicMock(return_value=""))
    def test_sync_feature_scope_namespace_dbs(self):
        """Verify sync_feature_scope propagates writes to per-namespace DBs on multi-ASIC."""
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_ns_db_0 = mock.MagicMock()
        mock_ns_db_1 = mock.MagicMock()
        feature_handler.ns_cfg_db = {'asic0': mock_ns_db_0, 'asic1': mock_ns_db_1}

        feature = featured.Feature('sflow', {
            'state': 'enabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'True',
        })

        # Host differs -> both host and namespaces should be written
        mock_db.get_entry.return_value = {
            'state': 'enabled',
            'has_per_asic_scope': 'False',
            'has_global_scope': 'False',
        }
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.get_entry.return_value = {
                'has_per_asic_scope': 'False',
                'has_global_scope': 'False',
            }
        feature_handler.sync_feature_scope(feature)

        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
                'has_per_asic_scope': 'True',
                'has_global_scope': 'True',
            })

        # Reset for next scenario
        mock_db.mod_entry.reset_mock()
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.reset_mock()

        # Host matches but namespaces are stale -> namespaces should still be written
        mock_db.get_entry.return_value = {
            'state': 'enabled',
            'has_per_asic_scope': 'True',
            'has_global_scope': 'True',
        }
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.get_entry.return_value = {
                'has_per_asic_scope': 'False',
                'has_global_scope': 'False',
            }
        feature_handler.sync_feature_scope(feature)

        mock_db.mod_entry.assert_not_called()
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
                'has_per_asic_scope': 'True',
                'has_global_scope': 'True',
            })


class TestHandlerDeregistration(TestCase):

    def test_handler_deregister_cleans_namespace_dbs(self):
        mock_db = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)

        mock_ns_cfg_db = mock.MagicMock()
        mock_ns_state_tbl = mock.MagicMock()
        feature_handler.ns_cfg_db = {'asic0': mock_ns_cfg_db}
        feature_handler.ns_feature_state_tbl = {'asic0': mock_ns_state_tbl}

        feature_handler._cached_config['sflow'] = featured.Feature('sflow', {'state': 'enabled'})

        feature_handler.handler('sflow', 'DEL', {})

        mock_feature_state_table._del.assert_called_once_with('sflow')
        mock_ns_cfg_db.set_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', None)
        mock_ns_state_tbl._del.assert_called_once_with('sflow')
        assert 'sflow' not in feature_handler._cached_config


class TestResyncFeatureStateNamespace(TestCase):

    def _make_handler(self, host_state, ns_get_entry):
        """Build a FeatureHandler with a mocked host DB and a single mocked namespace DB."""
        mock_db = mock.MagicMock()
        mock_db.get_entry.return_value = {'state': host_state} if host_state is not None else None
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)

        mock_ns_db = mock.MagicMock()
        mock_ns_db.get_entry.return_value = ns_get_entry
        feature_handler.ns_cfg_db = {'asic0': mock_ns_db}
        return feature_handler, mock_db, mock_ns_db

    def _feature(self, state):
        return featured.Feature('sflow', {'state': state, 'auto_restart': 'enabled'})

    def test_namespace_template_rendered_when_host_matches(self):
        # Host already 'enabled' (no host write), namespace still holds a template -> render it.
        feature_handler, mock_db, mock_ns_db = self._make_handler('enabled', {'state': '{{ some_template }}'})
        feature = self._feature('enabled')
        feature_handler._cached_config['sflow'] = feature

        feature_handler.resync_feature_state(feature)

        mock_db.mod_entry.assert_not_called()
        mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {'state': 'enabled'})

    def test_namespace_valid_state_not_overwritten(self):
        # Namespace already matches the rendered state -> idempotent, no write.
        feature_handler, mock_db, mock_ns_db = self._make_handler('enabled', {'state': 'enabled'})
        feature = self._feature('enabled')
        feature_handler._cached_config['sflow'] = feature

        feature_handler.resync_feature_state(feature)

        mock_db.mod_entry.assert_not_called()
        mock_ns_db.mod_entry.assert_not_called()

    def test_namespace_immutable_state_forced(self):
        # Immutable rendered state must be enforced in the namespace even if it holds a
        # concrete (non-template) differing value.
        feature_handler, mock_db, mock_ns_db = self._make_handler('always_enabled', {'state': 'disabled'})
        feature = self._feature('always_enabled')
        feature_handler._cached_config['sflow'] = feature

        feature_handler.resync_feature_state(feature)

        mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {'state': 'always_enabled'})

    def test_namespace_missing_state_repopulated(self):
        # Namespace entry exists but has no 'state' (e.g. only has_* written by
        # sync_feature_scope after a deregister/re-register) -> state must be repopulated.
        feature_handler, mock_db, mock_ns_db = self._make_handler('enabled', {'has_global_scope': 'True'})
        feature = self._feature('enabled')
        feature_handler._cached_config['sflow'] = feature

        feature_handler.resync_feature_state(feature)

        mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {'state': 'enabled'})

    def test_namespace_concrete_user_state_preserved(self):
        # Non-immutable rendered state: a concrete user-set namespace value that differs
        # must NOT be overwritten.
        feature_handler, mock_db, mock_ns_db = self._make_handler('disabled', {'state': 'enabled'})
        feature = self._feature('disabled')
        feature_handler._cached_config['sflow'] = feature

        feature_handler.resync_feature_state(feature)

        mock_db.mod_entry.assert_not_called()
        mock_ns_db.mod_entry.assert_not_called()


class TestHandlerReregistration(TestCase):

    def test_deregister_then_register_restores_namespace_state(self):
        # DEL removes the namespace entry; on the subsequent SET, sync_feature_scope is
        # stubbed and the namespace is simulated as a partial row (has_* only, no 'state'),
        # so this asserts that the SET-success resync_feature_state writes the namespace
        # 'state' back. (Handler-level test; sync_feature_scope itself is mocked out.)
        mock_db = mock.MagicMock()
        feature_handler = featured.FeatureHandler(mock_db, mock.MagicMock(), {}, False)
        # Make Feature() rendering deterministic regardless of the host's runtime metadata.
        feature_handler._device_running_config = {}

        mock_ns_cfg_db = mock.MagicMock()
        mock_ns_state_tbl = mock.MagicMock()
        feature_handler.ns_cfg_db = {'asic0': mock_ns_cfg_db}
        feature_handler.ns_feature_state_tbl = {'asic0': mock_ns_state_tbl}

        # 1) Deregister -> namespace entry deleted.
        feature_handler.handler('sflow', 'DEL', {})
        mock_ns_cfg_db.set_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', None)

        # 2) Re-register. Host already carries the state; the namespace was rebuilt partial
        #    (only has_* from sync_feature_scope) so it lacks 'state'.
        mock_db.get_entry.return_value = {'state': 'enabled'}
        mock_ns_cfg_db.get_entry.return_value = {'has_global_scope': 'True'}

        with mock.patch.object(feature_handler, 'update_systemd_config'), \
             mock.patch.object(feature_handler, 'update_feature_state', return_value=True), \
             mock.patch.object(feature_handler, 'sync_feature_scope'):
            feature_handler.handler('sflow', 'SET', {'state': 'enabled', 'auto_restart': 'enabled'})

        mock_ns_cfg_db.mod_entry.assert_any_call(featured.FEATURE_TBL, 'sflow', {'state': 'enabled'})


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
                                    ('FEATURE', 'mux')])
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
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]

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
                call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]               
        
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_portinit_timeout(self, mock_syslog, get_runtime):
        print(MockConfigDb.CONFIG_DB)
        MockSelect.NUM_TIMEOUT_TRIES = 1
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux')])
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
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_portinit_timeout_with_busy_port_table(self, mock_syslog, get_runtime):
        """Single-ASIC: a selector kept busy by PORT_TABLE traffic must not postpone the fallback."""
        MockConfigDb.CONFIG_DB['PORT_TABLE'] = {'Ethernet{}'.format(i): {'admin_status': 'up'} for i in range(8)}
        MockSelect.set_event_queue([('PORT_TABLE', 'Ethernet{}'.format(i)) for i in range(8)])
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            popen_mock.configure_mock(**{'communicate.return_value': ('output', 'error')})
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            assert not daemon.feature_handler.is_delayed_enabled
            try:
                daemon.start(0.0)   # deadline long past, but select never returns TIMEOUT
            except TimeoutError:
                pass
            assert daemon.feature_handler.is_delayed_enabled

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

    @staticmethod
    def _make_bare_daemon(is_multi_npu, ns_appl_db_conn=None):
        """FeatureDaemon built without __init__ (no live DB connections) for register_callbacks tests."""
        daemon = featured.FeatureDaemon.__new__(featured.FeatureDaemon)
        daemon.feature_handler = featured.FeatureHandler.__new__(featured.FeatureHandler)
        daemon.feature_handler.is_multi_npu = is_multi_npu
        daemon.cfg_db_conn = mock.MagicMock(name='cfg_db_conn')
        daemon.appl_db_conn = mock.MagicMock(name='host_appl_db_conn')
        daemon.ns_appl_db_conn = ns_appl_db_conn or {}
        daemon.subscribe = mock.MagicMock()
        return daemon

    def test_register_callbacks_multi_asic_subscribes_per_namespace(self, mock_syslog, get_runtime):
        """Multi-ASIC subscribes each namespace's PORT_TABLE and never the host APPL_DB."""
        conn0, conn1 = mock.MagicMock(name='asic0'), mock.MagicMock(name='asic1')
        daemon = self._make_bare_daemon(True, {'asic0': conn0, 'asic1': conn1})

        daemon.register_callbacks()

        port_calls = [c for c in daemon.subscribe.call_args_list if c.args[1] == featured.PORT_TBL]
        # One PORT_TABLE subscription per namespace, each on its own connector.
        self.assertEqual({c.kwargs.get('namespace'): c.args[0] for c in port_calls},
                         {'asic0': conn0, 'asic1': conn1})
        # The host APPL_DB must never be subscribed for ports on multi-ASIC.
        self.assertNotIn(daemon.appl_db_conn, [c.args[0] for c in port_calls])
        # FEATURE table is still subscribed exactly once, on the host config DB.
        feat_calls = [c for c in daemon.subscribe.call_args_list if c.args[1] == featured.FEATURE_TBL]
        self.assertEqual(len(feat_calls), 1)
        self.assertIs(feat_calls[0].args[0], daemon.cfg_db_conn)

    def test_register_callbacks_single_asic_subscribes_host(self, mock_syslog, get_runtime):
        """On single-ASIC, register_callbacks subscribes the host APPL_DB PORT_TABLE."""
        daemon = self._make_bare_daemon(False)

        daemon.register_callbacks()

        port_calls = [c for c in daemon.subscribe.call_args_list if c.args[1] == featured.PORT_TBL]
        self.assertEqual(len(port_calls), 1)
        self.assertIs(port_calls[0].args[0], daemon.appl_db_conn)


class TestWaitForServiceStable(TestCase):
    """Tests for wait_for_service_stable method that prevents orphaned containers."""

    def _create_handler(self):
        feature_state_table_mock = mock.Mock()
        device_cfg = {"DEVICE_METADATA": {"localhost": {"type": "ToRRouter"}}}
        handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_cfg, False)
        return handler

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_already_active(self, mock_sleep, mock_subprocess):
        """Service is already 'active' — should return immediately without polling."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"active\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "active"
        mock_sleep.assert_not_called()

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_inactive(self, mock_sleep, mock_subprocess):
        """Service is 'inactive' — should return immediately."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"inactive\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "inactive"
        mock_sleep.assert_not_called()

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_transitions_from_activating_to_active(self, mock_sleep, mock_subprocess):
        """Service starts in 'activating' then transitions to 'active' — should poll and return."""
        handler = self._create_handler()

        popen_mocks = []
        for state in [b"activating\n", b"activating\n", b"active\n"]:
            m = mock.Mock()
            m.communicate.return_value = (state, b"")
            m.returncode = 0
            popen_mocks.append(m)

        mock_subprocess.Popen.side_effect = popen_mocks

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "active"
        assert mock_sleep.call_count == 2

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    @mock.patch("featured.time.time")
    def test_service_activating_timeout(self, mock_time, mock_sleep, mock_subprocess):
        """Service stays 'activating' past timeout — should return 'activating'."""
        handler = self._create_handler()

        # Simulate time progressing past the timeout
        mock_time.side_effect = [0.0, 1.0, 2.0, 61.0]

        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"activating\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "activating"

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_failed(self, mock_sleep, mock_subprocess):
        """Service is 'failed' — should return immediately."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"failed\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "failed"
        mock_sleep.assert_not_called()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_disable_feature_calls_wait_for_service_stable(self, mock_syslog):
        """Verify disable_feature calls wait_for_service_stable before systemctl stop."""
        handler = self._create_handler()

        feat_cfg = {"state": "disabled", "auto_restart": "enabled"}
        feature = featured.Feature("bgp", feat_cfg)

        call_order = []

        def track_wait(unit):
            call_order.append(("wait", unit))
            return "active"

        def track_run_cmd(cmd, **kwargs):
            call_order.append(("cmd", cmd))

        with mock.patch.object(handler, "get_multiasic_feature_instances",
                               return_value=(["bgp@3"], ["service"])), \
             mock.patch.object(handler, "get_systemd_unit_state", return_value="enabled"), \
             mock.patch.object(handler, "wait_for_service_stable", side_effect=track_wait), \
             mock.patch("featured.run_cmd", side_effect=track_run_cmd), \
             mock.patch.object(handler, "set_feature_state"):

            handler.disable_feature(feature)

            # Verify wait was called before stop
            assert len(call_order) >= 2
            assert call_order[0] == ("wait", "bgp@3.service")
            assert call_order[1][0] == "cmd"
            assert "stop" in call_order[1][1]


class TestEnableFeatureGeneratedUnit(TestCase):
    """Tests that enable_feature() skips 'systemctl enable' for generated units.

    Running 'systemctl enable' on a unit whose UnitFileState is 'generated'
    (unit file only present under /run) fails, so it must not be attempted.
    """

    def _create_handler(self):
        feature_state_table_mock = mock.Mock()
        device_cfg = {"DEVICE_METADATA": {"localhost": {"type": "ToRRouter"}}}
        handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_cfg, False)
        return handler

    def _run_enable_feature(self, unit_file_state, feature_names=None, feature_suffixes=None):
        """Runs enable_feature() with a mocked unit file state and returns (result, cmds, set_state_mock)."""
        handler = self._create_handler()
        feature = featured.Feature("bgp", {"state": "enabled", "auto_restart": "enabled"})

        cmds = []

        with mock.patch.object(handler, "get_multiasic_feature_instances",
                               return_value=(feature_names or ["bgp"], feature_suffixes or ["service"])), \
             mock.patch.object(handler, "get_systemd_unit_state", return_value=unit_file_state), \
             mock.patch("featured.run_cmd", side_effect=lambda cmd, **kwargs: cmds.append(cmd)), \
             mock.patch.object(handler, "set_feature_state") as mocked_set_state:

            result = handler.enable_feature(feature)

        return result, cmds, mocked_set_state

    def test_enable_feature_skips_enable_for_generated_unit(self):
        """UnitFileState 'generated' — unmask and start are run, but enable is skipped."""
        result, cmds, mocked_set_state = self._run_enable_feature("generated")

        assert result is True
        assert ["sudo", "systemctl", "unmask", "bgp.service"] in cmds
        assert ["sudo", "systemctl", "start", "bgp.service"] in cmds
        assert not any("enable" in cmd for cmd in cmds)
        mocked_set_state.assert_called_once_with(mock.ANY, featured.FeatureHandler.FEATURE_STATE_ENABLED)

    def test_enable_feature_runs_enable_for_non_generated_unit(self):
        """UnitFileState 'disabled' — enable is run, confirming the skip is specific to 'generated'."""
        result, cmds, mocked_set_state = self._run_enable_feature("disabled")

        assert result is True
        assert cmds == [["sudo", "systemctl", "unmask", "bgp.service"],
                        ["sudo", "systemctl", "enable", "bgp.service"],
                        ["sudo", "systemctl", "start", "bgp.service"]]
        mocked_set_state.assert_called_once_with(mock.ANY, featured.FeatureHandler.FEATURE_STATE_ENABLED)

    def test_enable_feature_skips_enable_for_generated_timer_unit(self):
        """A generated feature with a .timer unit still unmasks both units and skips enable."""
        result, cmds, _ = self._run_enable_feature("generated",
                                                   feature_names=["bgp"],
                                                   feature_suffixes=["service", "timer"])

        assert result is True
        assert cmds == [["sudo", "systemctl", "unmask", "bgp.service"],
                        ["sudo", "systemctl", "unmask", "bgp.timer"],
                        ["sudo", "systemctl", "start", "bgp.timer"]]

    def test_enable_feature_skips_enable_for_generated_multiasic_instances(self):
        """Each generated per-ASIC instance is started without being enabled."""
        result, cmds, _ = self._run_enable_feature("generated",
                                                   feature_names=["bgp@0", "bgp@1"],
                                                   feature_suffixes=["service"])

        assert result is True
        assert not any("enable" in cmd for cmd in cmds)
        assert ["sudo", "systemctl", "start", "bgp@0.service"] in cmds
        assert ["sudo", "systemctl", "start", "bgp@1.service"] in cmds


class NsMockDBConnector(MockDBConnector):
    """MockDBConnector that knows its namespace; can fail to connect or be declared dead."""
    fail_appl_db_ns = set()
    dead_ns = set()

    def __init__(self, db, val, tcpFlag=False, name=None):
        if db == featured.APPL_DB and name in NsMockDBConnector.fail_appl_db_ns:
            raise RuntimeError("simulated APPL_DB connect failure for {}".format(name))
        super().__init__(db, val, tcpFlag, name)
        self.ns = name or ''


class NsMockSubscriberStateTable:
    """SubscriberStateTable mock keyed by (namespace, table)."""
    _fd = 0
    instances = []

    def __init__(self, conn, table, pop=None, pri=None):
        NsMockSubscriberStateTable._fd += 1
        self.fd, self.conn, self.table = NsMockSubscriberStateTable._fd, conn, table
        self.key = (conn.ns, table)
        self.next = None
        NsMockSubscriberStateTable.instances.append(self)

    def getFd(self):
        return self.fd

    def pop(self):
        key, op = self.next
        if self.table == featured.PORT_TBL:
            return key, op, {'lanes': '0'}
        return key, op, MockConfigDb.CONFIG_DB[self.table][key]


class NsMockSelect:
    """Select mock fed from a queue of (namespace, table, key, op) events or 'TIMEOUT'/'ERROR'
    markers; a subscriber on a dead redis yields ERROR until it is removed, and an empty
    queue ends the daemon loop with TimeoutError like MockSelect."""
    OBJECT, TIMEOUT, ERROR = 'OBJECT', 'TIMEOUT', 'ERROR'
    queue = []

    def __init__(self):
        self.subs = {}

    def addSelectable(self, s):
        self.subs[s.key] = s

    def removeSelectable(self, s):
        del self.subs[s.key]

    def select(self, timeout):
        if not NsMockSelect.queue:
            raise TimeoutError
        if any(s.conn.ns in NsMockDBConnector.dead_ns for s in self.subs.values()):
            return self.ERROR, None                     # closed subscription: ERROR until it is removed
        event = NsMockSelect.queue.pop(0)
        if event in (self.TIMEOUT, self.ERROR):
            return event, None
        ns, table, key, op = event
        s = self.subs[(ns, table)]
        s.next = (key, op)
        return self.OBJECT, s


MULTI_ASIC_DAEMON_CFG_DB = {
    'DEVICE_METADATA': {'localhost': {'type': 'LeafRouter'}},
    'FEATURE': {
        'lldp': {'state': 'enabled', 'delayed': 'True', 'auto_restart': 'enabled',
                 'has_global_scope': 'True', 'has_per_asic_scope': 'True'},
        'swss': {'state': 'enabled', 'delayed': 'False', 'auto_restart': 'enabled',
                 'has_global_scope': 'False', 'has_per_asic_scope': 'True'},
    },
}
NS = ['asic0', 'asic1', 'asic2']


def port_event(ns, key='PortInitDone', op='SET'):
    return (ns, featured.PORT_TBL, key, op)


@mock.patch("syslog.syslog", side_effect=syslog_side_effect)
@mock.patch('sonic_py_common.device_info.get_device_runtime_metadata',
            return_value={'DEVICE_RUNTIME_METADATA': {'ETHERNET_PORTS_PRESENT': True}})
@mock.patch('sonic_py_common.device_info.get_namespaces', return_value=NS)
@mock.patch('sonic_py_common.device_info.get_num_npus', return_value=3)
@mock.patch('sonic_py_common.device_info.is_multi_npu', return_value=True)
@mock.patch('sonic_py_common.device_info.get_num_dpus', return_value=0)
class TestFeatureDaemonMultiAsic(TestCase):
    """End-to-end multi-ASIC path: namespace discovery, per-namespace connectors,
    register_callbacks and start() dispatching by (namespace, table)."""

    def setUp(self):
        self.patcher = Patcher()
        self.patcher.setUp()
        self.addCleanup(self.patcher.tearDown)
        self.patcher.fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        MockConfigDb.CONFIG_DB = copy.deepcopy(MULTI_ASIC_DAEMON_CFG_DB)
        self.addCleanup(MockConfigDb.CONFIG_DB.clear)
        MockRestartWaiter.advancedReboot = False
        NsMockSelect.queue = []
        NsMockSubscriberStateTable.instances = []
        NsMockDBConnector.fail_appl_db_ns, NsMockDBConnector.dead_ns = set(), set()
        closed = lambda fd: any(s.getFd() == fd and s.conn.ns in NsMockDBConnector.dead_ns
                                for s in NsMockSubscriberStateTable.instances)
        for target, attr, value in ((featured.FeatureDaemon, 'subscription_closed', staticmethod(closed)),
                                    (featured, 'DBConnector', NsMockDBConnector),
                                    (featured, 'SonicDBConfig', mock.Mock()),
                                    (swsscommon, 'Select', NsMockSelect),
                                    (swsscommon, 'SubscriberStateTable', NsMockSubscriberStateTable)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _daemon(self):
        with mock.patch.object(featured, 'run_cmd'), \
             mock.patch.object(featured.FeatureHandler, 'get_systemd_unit_state', return_value='disabled'):
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
        return daemon

    @staticmethod
    def _run(daemon, init_time=None):
        """Drive start() until the event queue is empty; return (release mock, systemctl cmds)."""
        with mock.patch.object(daemon.feature_handler, 'enable_delayed_services',
                               wraps=daemon.feature_handler.enable_delayed_services) as release, \
             mock.patch.object(featured.FeatureHandler, 'get_systemd_unit_state', return_value='static'), \
             mock.patch.object(featured, 'run_cmd') as run_cmd, \
             mock.patch.object(featured.time, 'sleep'):
            try:
                daemon.start(init_time if init_time is not None else time.time())
            except TimeoutError:
                pass
        return release, [c.args[0] for c in run_cmd.call_args_list]

    def test_quorum_is_every_namespace_and_host_port_table_not_subscribed(self, *_):
        daemon = self._daemon()
        self.assertEqual(daemon.feature_handler.port_init_quorum_ns, set(NS))
        self.assertEqual(set(daemon.ns_appl_db_conn), set(NS))
        subscribed = {sub_key for (_, sub_key, _) in daemon.subscriber_map.values()}
        self.assertEqual(subscribed, {('', featured.FEATURE_TBL)} | {(ns, featured.PORT_TBL) for ns in NS})
        self.assertFalse(daemon.feature_handler.is_delayed_enabled)

    def test_release_after_all_namespaces_then_requorum_on_swss_restart(self, *_):
        daemon = self._daemon()
        NsMockSelect.queue = [port_event('asic0', 'Ethernet0'), port_event('asic0'), port_event('asic1')]
        release, cmds = self._run(daemon)
        release.assert_not_called()
        self.assertFalse(daemon.feature_handler.is_delayed_enabled)
        self.assertEqual(cmds, [])

        NsMockSelect.queue = [port_event('asic2')]
        release, cmds = self._run(daemon)
        release.assert_called_once()
        self.assertTrue(daemon.feature_handler.is_delayed_enabled)
        # only the delayed feature (lldp: host + one instance per ASIC) is started
        self.assertEqual([c[3] for c in cmds if c[2] == 'start'],
                         ['lldp.service', 'lldp@0.service', 'lldp@1.service', 'lldp@2.service'])

        # swss@1 restart: DEL starts nothing, SET re-enables without the other ASICs reporting again
        NsMockSelect.queue = [port_event('asic1', op='DEL')]
        release, cmds = self._run(daemon)
        release.assert_not_called()
        self.assertEqual(cmds, [])
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, {'asic1'})
        NsMockSelect.queue = [port_event('asic1')]
        release, cmds = self._run(daemon)
        release.assert_called_once()
        self.assertIn(['sudo', 'systemctl', 'start', 'lldp@1.service'], cmds)
        self.assertFalse(any(c[2] in ('stop', 'disable', 'mask') for c in cmds))

    def test_failed_connector_keeps_namespace_pending_until_timeout(self, *_):
        NsMockDBConnector.fail_appl_db_ns = {'asic1'}
        daemon = self._daemon()
        self.assertEqual(set(daemon.ns_appl_db_conn), {'asic0', 'asic2'})
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, set(NS))
        NsMockSelect.queue = [port_event('asic0'), port_event('asic2')]
        release, _ = self._run(daemon)
        release.assert_not_called()                      # asic1 cannot be observed -> no early release
        NsMockSelect.queue = [NsMockSelect.TIMEOUT]
        release, _ = self._run(daemon, init_time=time.time() - featured.PORT_INIT_TIMEOUT_SEC - 1)
        release.assert_called_once()                      # ...the timeout backstop releases

    def test_timeout_fires_while_selector_is_busy(self, *_):
        """Continuous PORT_TABLE traffic (no idle select) must not postpone the fallback."""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event('asic0', 'Ethernet{}'.format(i)) for i in range(20)]
        release, _ = self._run(daemon, init_time=time.time() - featured.PORT_INIT_TIMEOUT_SEC - 1)
        release.assert_called_once()
        self.assertTrue(daemon.feature_handler.is_delayed_enabled)

    def test_swss_restart_after_timeout_release_still_reenables(self, *_):
        """After the fallback release, an swss restart on a healthy ASIC must still re-enable."""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event('asic0'), port_event('asic1')]
        release, _ = self._run(daemon)
        release.assert_not_called()
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, {'asic2'})
        NsMockSelect.queue = [NsMockSelect.TIMEOUT]
        release, _ = self._run(daemon, init_time=time.time() - featured.PORT_INIT_TIMEOUT_SEC - 1)
        release.assert_called_once()
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, set())   # asic2 given up on
        NsMockSelect.queue = [port_event('asic0', op='DEL'), port_event('asic0')]
        release, cmds = self._run(daemon)
        release.assert_called_once()
        self.assertIn(['sudo', 'systemctl', 'start', 'lldp@0.service'], cmds)

    def test_failed_instance_does_not_block_other_instances(self, *_):
        """A failing lldp@0 must not keep lldp@1 from starting; the feature is reported failed."""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event(ns) for ns in NS]
        self._run(daemon)
        NsMockSelect.queue = [port_event('asic0', op='DEL'), port_event('asic1', op='DEL'), port_event('asic1')]
        def run_cmd(cmd, log_err=True, raise_exception=False):
            if cmd[2:] == ['start', 'lldp@0.service']:
                raise Exception('lldp@0 start failed')
        states = []
        with mock.patch.object(featured.FeatureHandler, 'get_systemd_unit_state', return_value='static'), \
             mock.patch.object(featured, 'run_cmd', side_effect=run_cmd) as run_cmd_mock, \
             mock.patch.object(daemon.feature_handler, 'set_feature_state', side_effect=lambda f, s: states.append(s)):
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
        starts = [c.args[0][3] for c in run_cmd_mock.call_args_list if c.args[0][2] == 'start']
        self.assertIn('lldp@1.service', starts)
        self.assertIn('lldp@2.service', starts)
        self.assertEqual(states[-1], featured.FeatureHandler.FEATURE_STATE_FAILED)

    def test_replayed_markers_release_immediately(self, *_):
        """Markers present at subscription time are replayed as SETs and release at once."""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event(ns) for ns in NS]
        release, _ = self._run(daemon, init_time=time.time())
        release.assert_called_once()

    def test_warm_boot_release_then_quorum_events_are_harmless(self, *_):
        MockRestartWaiter.advancedReboot = True
        daemon = self._daemon()
        self.assertTrue(daemon.feature_handler.is_delayed_enabled)
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, set())
        NsMockSelect.queue = [port_event(ns) for ns in NS]                       # replayed markers
        release, cmds = self._run(daemon)
        release.assert_not_called()                                              # nothing to do
        NsMockSelect.queue = [port_event('asic1', op='DEL'), port_event('asic1')]   # swss@1 restart
        release, cmds = self._run(daemon)
        release.assert_called_once()
        self.assertFalse(any(c[2] in ('stop', 'disable', 'mask') for c in cmds))

    def test_dead_namespace_redis_is_dropped_and_dispatch_continues(self, *_):
        """A dead namespace redis: its subscriber is dropped, other tables are served again
        and the namespace stays pending. (Writes to that namespace's own DBs would still
        fail, as in the base; not modelled here.)"""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event('asic0'), port_event('asic1')]
        self._run(daemon)
        NsMockDBConnector.dead_ns = {'asic2'}
        MockConfigDb.CONFIG_DB['FEATURE']['swss']['state'] = 'disabled'   # a user disables a feature
        NsMockSelect.queue = [('', featured.FEATURE_TBL, 'swss', 'SET')]
        with mock.patch.object(featured.FeatureHandler, 'wait_for_service_stable', return_value='active'):
            release, cmds = self._run(daemon)
        self.assertNotIn(('asic2', featured.PORT_TBL), {sk for (_, sk, _) in daemon.subscriber_map.values()})
        self.assertNotIn(('asic2', featured.PORT_TBL), daemon.callbacks)
        self.assertIn(['sudo', 'systemctl', 'stop', 'swss@0.service'], cmds)   # FEATURE event was served
        release.assert_not_called()
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, {'asic2'})

    def test_dead_redis_of_ready_namespace_before_release_is_pending_again(self, *_):
        """A ready namespace whose redis dies before the release is pending again."""
        daemon = self._daemon()
        NsMockSelect.queue = [port_event('asic0')]
        self._run(daemon)
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, {'asic1', 'asic2'})
        NsMockDBConnector.dead_ns = {'asic0'}
        NsMockSelect.queue = [port_event('asic1'), port_event('asic2')]
        release, _ = self._run(daemon)
        release.assert_not_called()
        self.assertEqual(daemon.feature_handler.pending_port_init_ns, {'asic0'})
        NsMockSelect.queue = [NsMockSelect.TIMEOUT]
        release, _ = self._run(daemon, init_time=time.time() - featured.PORT_INIT_TIMEOUT_SEC - 1)
        release.assert_called_once()

    def test_select_error_without_dead_subscriber_backs_off(self, *_):
        daemon = self._daemon()
        NsMockSelect.queue = [NsMockSelect.ERROR, NsMockSelect.ERROR]
        with mock.patch.object(featured.time, 'sleep') as sleep, \
             mock.patch.object(featured, 'run_cmd'):
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(daemon.subscriber_map), 4)  # nothing dropped

    def test_all_subscriptions_lost_exits(self, *_):
        """With every subscription gone, featured logs once and exits instead of looping."""
        daemon = self._daemon()
        NsMockDBConnector.dead_ns = {''} | set(NS)
        NsMockSelect.queue = [NsMockSelect.ERROR]
        with mock.patch.object(featured.time, 'sleep') as sleep, \
             mock.patch.object(featured, 'run_cmd'), self.assertRaises(SystemExit):
            daemon.start(time.time())
        self.assertEqual(daemon.subscriber_map, {})
        sleep.assert_not_called()


class TestSubscriptionClosed(TestCase):
    """The socket probe behind drop_dead_subscribers(), against real sockets."""

    def setUp(self):
        self.sock, self.peer = socket.socketpair()
        self.addCleanup(self.sock.close)
        self.addCleanup(self.peer.close)

    def test_idle_connection_is_open(self):
        self.assertFalse(featured.FeatureDaemon.subscription_closed(self.sock.fileno()))

    def test_pending_data_is_open_and_left_unread(self):
        self.peer.send(b'x')
        self.assertFalse(featured.FeatureDaemon.subscription_closed(self.sock.fileno()))
        self.assertEqual(self.sock.recv(1), b'x')

    def test_peer_closed_is_closed(self):
        self.peer.close()
        self.assertTrue(featured.FeatureDaemon.subscription_closed(self.sock.fileno()))

    def test_unusable_fd_is_closed_without_raising(self):
        fd = os.dup(self.sock.fileno())
        os.close(fd)
        self.assertTrue(featured.FeatureDaemon.subscription_closed(fd))


class TestPendingFeatureUpdates(TestCase):
    """Exercise real initialization/SET/release paths with failing systemd commands."""

    def setUp(self):
        for name, value in [('is_multi_npu', False), ('get_num_npus', 1),
                            ('get_num_dpus', 0), ('get_device_runtime_metadata', {})]:
            patcher = mock.patch.object(device_info, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {'FEATURE': {}}
        MockConfigDb.set_config_db(self.config)
        self.state_table = mock.Mock()
        self.handler = featured.FeatureHandler(MockConfigDb(), self.state_table, {}, False)
        self.unit_states = {'teamd.service': 'disabled'}
        self.commands = []
        self.failure = None
        self.fail_after_effect = False
        for name in ['update_systemd_config', 'reload_systemd_config', 'wait_for_service_stable']:
            patcher = mock.patch.object(self.handler, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.handler, 'get_systemd_unit_state',
                                    side_effect=lambda unit: self.unit_states[unit])
        patcher.start()
        self.addCleanup(patcher.stop)
        # Keep run_cmd real, including its intentional tolerance of enable errors.
        patcher = mock.patch('featured.subprocess.run', side_effect=self.run_systemctl)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_systemctl(self, cmd, **kwargs):
        action, unit = cmd[2:]
        self.commands.append((action, unit))
        fail = self.failure == (action, unit)
        if not fail or self.fail_after_effect:
            if action == 'unmask' and self.unit_states[unit] == 'masked':
                self.unit_states[unit] = 'disabled'
            elif action in ('enable', 'disable', 'mask'):
                self.unit_states[unit] = {'enable': 'enabled', 'disable': 'disabled',
                                          'mask': 'masked'}[action]
        if fail:
            self.failure = None
            raise subprocess.CalledProcessError(1, cmd, output='', stderr='injected failure')
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    def initialize(self, state, delayed=False):
        cfg = {'state': state, 'delayed': str(delayed), 'auto_restart': 'enabled',
               'has_global_scope': 'True', 'has_per_asic_scope': 'False',
               'has_per_dpu_scope': 'False'}
        self.config['FEATURE']['teamd'] = cfg.copy()
        self.handler.sync_state_field({'teamd': cfg})
        return cfg

    def send_set(self, cfg):
        self.config['FEATURE']['teamd'] = cfg.copy()
        self.handler.handler('teamd', 'SET', cfg)

    @parameterized.expand([
        (state, delayed) for state in ('enabled', 'always_enabled', 'disabled', 'always_disabled')
        for delayed in (False, True)
    ])
    def test_initial_or_delayed_failure_retries_same_set(self, state, delayed):
        enabling = state in ('enabled', 'always_enabled')
        operation = 'start' if enabling else 'mask'
        self.unit_states['teamd.service'] = 'disabled' if enabling else 'enabled'
        self.failure = (operation, 'teamd.service')
        cfg = self.initialize(state, delayed)
        cached = self.handler._cached_config['teamd']
        assert cached.state == state
        assert cached.delayed == delayed
        assert cached.auto_restart == 'enabled'
        assert cached.has_global_scope is True
        assert cached.has_per_asic_scope is False
        if delayed:
            assert self.commands == []
            self.send_set(cfg)
            assert self.commands == []
            self.handler.port_listener('PortInitDone', 'SET', {})
        assert self.commands.count((operation, 'teamd.service')) == 1
        self.state_table.set.assert_called_with('teamd', [('state', 'failed')])
        commands_before_retry = len(self.commands)
        self.send_set(cfg)
        if enabling:
            assert self.commands[commands_before_retry:] == [('start', 'teamd.service')]
        assert self.commands.count((operation, 'teamd.service')) == 2
        self.state_table.set.assert_called_with(
            'teamd', [('state', 'enabled' if enabling else 'disabled')])
        assert 'teamd' not in self.handler._features_pending_update
        completed_commands = self.commands.copy()
        self.send_set(cfg)
        assert self.commands == completed_commands

    @parameterized.expand([
        ('unmask', 'enabled', 'masked', False),
        ('stop', 'disabled', 'enabled', False),
        ('disable', 'disabled', 'enabled', False),
        ('disable', 'disabled', 'enabled', True),
        ('mask', 'disabled', 'enabled', False),
        ('mask', 'disabled', 'enabled', True),
    ])
    def test_partial_service_operation_is_retried(self, action, state, unit_state, after_effect):
        self.unit_states['teamd.service'] = unit_state
        self.failure = (action, 'teamd.service')
        self.fail_after_effect = after_effect
        cfg = self.initialize(state)
        assert self.commands.count((action, 'teamd.service')) == 1
        self.send_set(cfg)
        assert self.commands.count((action, 'teamd.service')) == 2
        assert self.commands[-1] == ('start' if state == 'enabled' else 'mask', 'teamd.service')
        self.state_table.set.assert_called_with('teamd', [('state', state)])

    def test_del_clears_pending_before_reregistration(self):
        self.failure = ('start', 'teamd.service')
        cfg = self.initialize('enabled')
        assert 'teamd' in self.handler._features_pending_update
        self.handler.handler('teamd', 'DEL', {})
        assert 'teamd' not in self.handler._features_pending_update
        assert 'teamd' not in self.handler._cached_config
        self.state_table._del.assert_called_once_with('teamd')
        commands = self.commands.copy()
        self.send_set(cfg)
        # DEL removed the retry intent; enabled units follow the original fast path.
        assert self.commands == commands

    @parameterized.expand([('enabled', 'start'), ('disabled', 'stop')])
    def test_failed_instance_does_not_block_others_or_clear_pending(self, state, action):
        cfg = {'state': state, 'has_global_scope': 'False', 'has_per_asic_scope': 'True'}
        self.config['FEATURE']['teamd'] = cfg.copy()
        self.handler.is_multi_npu = True
        units = ['teamd@0.service', 'teamd@1.service']
        self.unit_states.update({unit: 'disabled' if state == 'enabled' else 'enabled'
                                 for unit in units})
        self.unit_states['teamd.service'] = 'masked'
        with mock.patch.object(device_info, 'get_num_npus', return_value=2):
            self.failure = (action, units[0])
            self.handler.sync_state_field({'teamd': cfg})
            assert (action, units[1]) in self.commands
            self.state_table.set.assert_called_with('teamd', [('state', 'failed')])
            assert 'teamd' in self.handler._features_pending_update
            # A second failure must leave pending set, even after another instance succeeds.
            self.failure = (action, units[0])
            self.send_set(cfg)
            assert self.commands.count((action, units[0])) == 2
            assert 'teamd' in self.handler._features_pending_update
            self.send_set(cfg)
        assert self.commands.count((action, units[0])) == 3
        assert 'teamd' not in self.handler._features_pending_update
        self.state_table.set.assert_called_with('teamd', [('state', state)])

    @parameterized.expand([('enabled', 'start'), ('disabled', 'mask')])
    def test_pending_does_not_bypass_immutable_restrictions(self, state, action):
        self.unit_states['teamd.service'] = 'disabled' if state == 'enabled' else 'enabled'
        self.failure = (action, 'teamd.service')
        cfg = self.initialize('always_' + state)
        commands = self.commands.copy()
        self.send_set(dict(cfg, state=state))
        assert self.commands == commands
        assert self.config['FEATURE']['teamd']['state'] == 'always_' + state
        assert 'teamd' in self.handler._features_pending_update
        # Switching between the two immutable states remains allowed.
        other = 'always_disabled' if state == 'enabled' else 'always_enabled'
        self.send_set(dict(cfg, state=other))
        assert self.handler._cached_config['teamd'].state == other
        assert 'teamd' not in self.handler._features_pending_update

    @parameterized.expand([('port',), ('timeout',), ('advanced_boot',)])
    def test_delayed_release_retries_pending_operation(self, release):
        self.failure = ('start', 'teamd.service')
        cfg = self.initialize('enabled')
        # A subsequent SET defers the unfinished operation until an existing release event.
        self.send_set(dict(cfg, delayed='True'))
        assert self.commands.count(('start', 'teamd.service')) == 1
        assert 'teamd' in self.handler._features_pending_update
        if release == 'port':
            self.handler.port_listener('PortInitDone', 'SET', {})
        elif release == 'timeout':
            self.handler.handle_port_table_timeout()
        else:
            self.handler.is_advanced_boot = True
            self.handler.handle_adv_boot()
        assert self.commands.count(('start', 'teamd.service')) == 2
        assert 'teamd' not in self.handler._features_pending_update

    def test_generated_unit_retry_does_not_enable_unit(self):
        self.unit_states['teamd.service'] = 'generated'
        self.failure = ('start', 'teamd.service')
        cfg = self.initialize('enabled')
        self.send_set(cfg)
        assert self.commands.count(('start', 'teamd.service')) == 2
        assert ('enable', 'teamd.service') not in self.commands
        assert 'teamd' not in self.handler._features_pending_update

    @parameterized.expand([('enabled', 'start'), ('disabled', 'mask')])
    def test_failed_state_change_retries_same_set(self, target_state, action):
        cfg = self.initialize('disabled' if target_state == 'enabled' else 'enabled')
        self.commands.clear()
        self.failure = (action, 'teamd.service')
        target_cfg = dict(cfg, state=target_state)
        self.send_set(target_cfg)
        self.state_table.set.assert_called_with('teamd', [('state', 'failed')])
        self.send_set(target_cfg)
        assert self.commands.count((action, 'teamd.service')) == 2
        self.state_table.set.assert_called_with('teamd', [('state', target_state)])
        commands = self.commands.copy()
        self.send_set(target_cfg)
        assert self.commands == commands

    def test_tolerated_enable_error_does_not_leave_pending(self):
        # Units copied into /run may reject enable; a successful start is sufficient.
        self.failure = ('enable', 'teamd.service')
        cfg = self.initialize('enabled')
        assert self.commands == [('unmask', 'teamd.service'),
                                 ('enable', 'teamd.service'), ('start', 'teamd.service')]
        self.state_table.set.assert_called_with('teamd', [('state', 'enabled')])
        assert 'teamd' not in self.handler._features_pending_update
        commands = self.commands.copy()
        self.send_set(cfg)
        assert self.commands == commands

    @parameterized.expand([('enabled',), ('disabled',)])
    def test_excluded_feature_does_not_leave_retry_pending(self, state):
        cfg = {'state': state}
        self.config['FEATURE']['telemetry'] = cfg.copy()
        self.unit_states['telemetry.service'] = 'enabled'
        self.handler.sync_state_field({'telemetry': cfg})
        assert self.commands == []
        assert 'telemetry' not in self.handler._features_pending_update
