import builtins
import importlib.machinery
import importlib.util
import os
import sys
import types
from unittest import mock

import pytest


scripts_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts')
loader = importlib.machinery.SourceFileLoader(
    'aaastatsd', os.path.join(scripts_path, 'aaastatsd'))
spec = importlib.util.spec_from_loader(loader.name, loader)
aaastatsd = importlib.util.module_from_spec(spec)

watchdog = types.ModuleType('watchdog')
watchdog_observers = types.ModuleType('watchdog.observers')
watchdog_events = types.ModuleType('watchdog.events')
watchdog_observers.Observer = mock.Mock
watchdog_events.FileSystemEventHandler = object
swsscommon = types.ModuleType('swsscommon')
swsscommon_module = types.ModuleType('swsscommon.swsscommon')
swsscommon_module.ConfigDBConnector = mock.Mock

with mock.patch.dict(sys.modules, {
        'swsscommon': swsscommon,
        'swsscommon.swsscommon': swsscommon_module,
        'watchdog': watchdog,
        'watchdog.observers': watchdog_observers,
        'watchdog.events': watchdog_events,
}):
    loader.exec_module(aaastatsd)


@pytest.mark.parametrize('server', ['', '.', '..', 'nested/name',
                                    'nested/../192.0.2.1', 'bad\x00name'])
def test_stats_path_rejects_invalid_server_names(server):
    with mock.patch.object(aaastatsd.syslog, 'syslog'):
        assert aaastatsd.radius_stats_file_path(server) is None


def test_stats_path_accepts_server_address():
    expected = os.path.realpath(os.path.join(
        aaastatsd.RADIUS_PAM_AUTH_CONF_STATS_DIR, '2001:db8::1'))

    assert aaastatsd.radius_stats_file_path('2001:db8::1') == expected


@pytest.mark.parametrize('method_name', ['create_file', 'handle_update'])
def test_invalid_server_name_does_not_access_file(method_name):
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True'}

    with mock.patch.object(builtins, 'open') as mocked_open, \
            mock.patch.object(aaastatsd.os.path, 'exists') as mocked_exists, \
            mock.patch.object(aaastatsd.os, 'chmod') as mocked_chmod:
        getattr(radius_stats, method_name)('nested/name')

    mocked_open.assert_not_called()
    mocked_exists.assert_not_called()
    mocked_chmod.assert_not_called()


def test_create_file_accepts_server_address(tmp_path):
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True'}

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep), \
            mock.patch.object(radius_stats, 'handle_update') as mocked_update:
        radius_stats.create_file('2001:db8::1')

    assert (tmp_path / '2001:db8::1').exists()
    mocked_update.assert_called_once_with('2001:db8::1')


def test_traversal_alias_cannot_unlink_another_servers_file(tmp_path):
    stats_file = tmp_path / '192.0.2.1'
    stats_file.write_text('counter data')
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'False'}

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep):
        radius_stats.create_file('nested/../192.0.2.1')

    assert stats_file.read_text() == 'counter data'


def test_same_directory_symlink_cannot_unlink_another_servers_file(tmp_path):
    stats_file = tmp_path / '192.0.2.1'
    stats_file.write_text('counter data')
    alias = tmp_path / '192.0.2.2'
    alias.symlink_to(stats_file)
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'False'}

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep):
        radius_stats.create_file('192.0.2.2')

    assert stats_file.read_text() == 'counter data'
    assert alias.is_symlink()


def test_same_directory_symlink_is_not_read_or_written(tmp_path):
    stats_file = tmp_path / '192.0.2.1'
    stats_file.write_text('counter data')
    alias = tmp_path / '192.0.2.2'
    alias.symlink_to(stats_file)
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True'}

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep):
        with mock.patch.object(radius_stats, 'handle_update') as update:
            radius_stats.create_file('192.0.2.2')
            update.assert_not_called()
        with mock.patch.object(aaastatsd.os, 'listdir', return_value=['192.0.2.2']):
            radius_stats.handle_clear()
        with mock.patch.object(builtins, 'open') as file_open:
            radius_stats.handle_update('192.0.2.2')
            file_open.assert_not_called()

    assert stats_file.read_text() == 'counter data'
    assert alias.is_symlink()


def test_clear_only_truncates_local_stats_files(tmp_path):
    stats_dir = tmp_path / 'statistics'
    stats_dir.mkdir()
    local_file = stats_dir / '192.0.2.1'
    local_file.write_text('counter data')
    other_file = tmp_path / 'other'
    other_file.write_text('keep data')
    (stats_dir / '192.0.2.2').symlink_to(other_file)

    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(stats_dir) + os.path.sep):
        radius_stats.handle_clear()

    assert local_file.read_text() == ''
    assert other_file.read_text() == 'keep data'


def test_radius_statistics_uses_unix_socket_for_counters_db():
    counters_db = mock.Mock()

    with mock.patch.object(aaastatsd, 'ConfigDBConnector',
                           return_value=counters_db) as connector, \
            mock.patch.object(aaastatsd, 'RadiusCountersDbMon') as db_monitor, \
            mock.patch.object(aaastatsd, 'RadiusStatsFileMon'), \
            mock.patch.object(aaastatsd.syslog, 'syslog'):
        aaastatsd.RadiusStatistics(mock.Mock(), {}, {})

    connector.assert_called_once_with(use_unix_socket_path=True)
    counters_db.db_connect.assert_called_once_with(
        'COUNTERS_DB', wait_for_init=False, retry_on=True)
    db_monitor.return_value.start.assert_called_once_with()


def test_radius_update_uses_unix_socket_for_counters_db(tmp_path):
    counters_db = mock.Mock()
    radius_stats = aaastatsd.RadiusStatistics.__new__(
        aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True'}
    radius_stats.radius_counter_names = []

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep), \
            mock.patch.object(aaastatsd, 'ConfigDBConnector',
                              return_value=counters_db) as connector:
        radius_stats.handle_update('192.0.2.1')

    connector.assert_called_once_with(use_unix_socket_path=True)
    counters_db.db_connect.assert_called_once_with(
        'COUNTERS_DB', wait_for_init=False, retry_on=False)
    counters_db.set_entry.assert_called_once_with(
        'RADIUS_SERVER_STATS', '192.0.2.1', None)


def test_aaa_stats_daemon_uses_unix_socket_for_config_db():
    config_db = mock.Mock()
    config_db.get_table.side_effect = [{}, {}]

    with mock.patch.object(aaastatsd, 'ConfigDBConnector',
                           return_value=config_db) as connector, \
            mock.patch.object(aaastatsd, 'RadiusStatistics') as radius_stats, \
            mock.patch.object(aaastatsd.syslog, 'syslog'):
        daemon = aaastatsd.AAAStatsDaemon()

    connector.assert_called_once_with(use_unix_socket_path=True)
    config_db.connect.assert_called_once_with(
        wait_for_init=True, retry_on=True)
    radius_stats.assert_called_once_with(config_db, {}, {})
    assert daemon.config_db is config_db
