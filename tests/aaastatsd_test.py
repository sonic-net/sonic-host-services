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


def test_file_handler_updates_only_modified_file():
    radius_stats = mock.Mock()
    handler = aaastatsd.RadiusStatsFileHandler(radius_stats)
    event = mock.Mock(is_directory=False, src_path='/stats/192.0.2.1')

    handler.on_modified(event)

    radius_stats.handle_update.assert_called_once_with('192.0.2.1')


def test_file_handler_ignores_directory_modification():
    radius_stats = mock.Mock()
    handler = aaastatsd.RadiusStatsFileHandler(radius_stats)
    event = mock.Mock(is_directory=True, src_path='/stats')

    handler.on_modified(event)

    radius_stats.handle_update.assert_not_called()


@pytest.mark.parametrize('value, expected', [
    ('true', True),
    ('True', True),
    ('TRUE', True),
    ('false', False),
    ('False', False),
    (None, False),
])
def test_statistics_enabled_is_case_insensitive(value, expected):
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {}
    if value is not None:
        radius_stats.radius_global['statistics'] = value

    assert radius_stats.statistics_enabled() is expected


def test_initialization_loads_config_and_cleans_stale_stats():
    address = '192.0.2.1'
    config_db = mock.Mock()
    global_config = {'global': {'statistics': 'True'}}
    server_config = {address: {'auth_port': '1812'}}

    with mock.patch.object(aaastatsd.RadiusStatistics,
                           'create_file') as create_file, \
            mock.patch.object(aaastatsd.RadiusStatistics,
                              'remove_stale_stats') as remove_stale_stats, \
            mock.patch.object(aaastatsd,
                              'ConfigDBConnector') as connector_class, \
            mock.patch.object(aaastatsd,
                              'RadiusCountersDbMon') as db_monitor_class, \
            mock.patch.object(aaastatsd,
                              'RadiusStatsFileMon') as file_monitor_class, \
            mock.patch.object(aaastatsd.syslog, 'syslog'):
        radius_stats = aaastatsd.RadiusStatistics(
            config_db, global_config, server_config)

    assert radius_stats.radius_global == {'statistics': 'True'}
    assert radius_stats.radius_servers == {address: {'auth_port': '1812'}}
    create_file.assert_called_once_with(address)
    remove_stale_stats.assert_called_once_with()
    connector_class.return_value.db_connect.assert_called_once_with(
        'COUNTERS_DB', wait_for_init=False, retry_on=True)
    db_monitor_class.return_value.start.assert_called_once_with()
    file_monitor_class.assert_called_once_with(radius_stats)


@pytest.mark.parametrize('data, expected', [
    ({}, {'statistics': 'False'}),
    ({'statistics': 'True', 'auth_type': 'pap'},
     {'statistics': 'True', 'auth_type': 'pap'}),
])
def test_radius_global_update_replaces_global_config(data, expected):
    address = '192.0.2.1'
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True', 'old': 'value'}
    radius_stats.radius_servers = {address: {'auth_port': '1812'}}

    with mock.patch.object(radius_stats, 'create_file') as create_file:
        radius_stats.radius_global_update('global', data)

    assert radius_stats.radius_global == expected
    create_file.assert_called_once_with(address)


def test_radius_server_update_adds_server_and_creates_file():
    address = '192.0.2.1'
    server_config = {'auth_port': '1812'}
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_servers = {}

    with mock.patch.object(radius_stats, 'create_file') as create_file:
        radius_stats.radius_server_update(address, server_config)

    assert radius_stats.radius_servers == {address: server_config}
    create_file.assert_called_once_with(address)


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


def test_delete_server_removes_statistics_without_recreating_file():
    address = '192.0.2.1'
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_servers = {address: {'auth_port': '1812'}}

    with mock.patch.object(radius_stats, 'remove_stats') as remove_stats, \
            mock.patch.object(radius_stats, 'create_file') as create_file:
        radius_stats.radius_server_update(address, {})

    assert address not in radius_stats.radius_servers
    remove_stats.assert_called_once_with(address)
    create_file.assert_not_called()


def test_create_file_removes_statistics_when_disabled():
    address = '192.0.2.1'
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'false'}

    with mock.patch.object(radius_stats, 'remove_stats') as remove_stats, \
            mock.patch.object(builtins, 'open') as file_open:
        radius_stats.create_file(address)

    remove_stats.assert_called_once_with(address)
    file_open.assert_not_called()


def test_remove_stats_removes_file_and_counters_entry(tmp_path):
    address = '192.0.2.1'
    stats_file = tmp_path / address
    stats_file.write_text('counter data')
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep), \
            mock.patch.object(radius_stats, 'update_counters_db') as update_db:
        radius_stats.remove_stats(address)

    assert not stats_file.exists()
    update_db.assert_called_once_with(address, None)


def test_remove_stats_rejects_invalid_server_name():
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)

    with mock.patch.object(radius_stats, 'update_counters_db') as update_db, \
            mock.patch.object(aaastatsd.syslog, 'syslog'):
        radius_stats.remove_stats('nested/name')

    update_db.assert_not_called()


def test_update_counters_db_writes_and_closes_connection():
    address = '192.0.2.1'
    entry = {'access_requests': '1'}
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    counters_db = mock.Mock()

    with mock.patch.object(aaastatsd, 'ConfigDBConnector',
                           return_value=counters_db):
        radius_stats.update_counters_db(address, entry)

    counters_db.db_connect.assert_called_once_with(
        'COUNTERS_DB', wait_for_init=False, retry_on=False)
    counters_db.set_entry.assert_called_once_with(
        'RADIUS_SERVER_STATS', address, entry)
    counters_db.close.assert_called_once_with(counters_db.COUNTERS_DB)


def test_remove_stale_stats_keeps_configured_server(tmp_path):
    configured = '192.0.2.1'
    stale = '192.0.2.2'
    (tmp_path / configured).write_text('configured')
    (tmp_path / stale).write_text('stale')
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_servers = {configured: {'auth_port': '1812'}}

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep), \
            mock.patch.object(radius_stats, 'remove_stats') as remove_stats:
        radius_stats.remove_stale_stats()

    remove_stats.assert_called_once_with(stale)


def test_remove_stale_stats_ignores_missing_directory(tmp_path):
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_servers = {}
    missing_directory = tmp_path / 'missing'

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(missing_directory) + os.path.sep), \
            mock.patch.object(radius_stats, 'remove_stats') as remove_stats:
        radius_stats.remove_stale_stats()

    remove_stats.assert_not_called()


def test_handle_update_reads_statistics_and_updates_counters_db(tmp_path):
    address = '192.0.2.1'
    (tmp_path / address).write_text('0 7')
    radius_stats = aaastatsd.RadiusStatistics.__new__(aaastatsd.RadiusStatistics)
    radius_stats.radius_global = {'statistics': 'True'}
    radius_stats.radius_counter_names = ['counter_0', 'access_requests']

    with mock.patch.object(aaastatsd, 'RADIUS_PAM_AUTH_CONF_STATS_DIR',
                           str(tmp_path) + os.path.sep), \
            mock.patch.object(radius_stats, 'update_counters_db') as update_db:
        radius_stats.handle_update(address)

    update_db.assert_called_once_with(address, {
        'counter_0': '0',
        'access_requests': '7',
    })


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
