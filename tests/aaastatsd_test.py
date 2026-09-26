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


@pytest.mark.parametrize('server', ['', '.', 'nested/name'])
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
