import runpy
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


SERVER = Path(__file__).resolve().parents[1] / 'scripts' / 'sonic-host-server'
HANDLERS = (
    ('config_engine', 'Config', 'config'),
    ('gcu', 'GCU', 'gcu'),
    ('host_service', 'HostService', 'host_service'),
    ('reboot', 'Reboot', 'reboot'),
    ('showtech', 'Showtech', 'showtech'),
    ('systemd_service', 'SystemdService', 'systemd'),
    ('image_service', 'ImageService', 'image_service'),
    ('docker_service', 'DockerService', 'docker_service'),
    ('debug_info', 'DebugArtifactCollector', 'debug_info'),
    ('gnoi_reset', 'GnoiReset', 'gnoi_reset'),
    ('ssh_mgmt', 'SshMgmt', 'ssh_mgmt'),
    ('gnsi_console', 'GnsiConsole', 'gnsi_console'),
    ('glome', 'Glome', 'glome'),
)


@pytest.fixture
def server_environment(monkeypatch):
    dbus = mock.MagicMock()
    gi = mock.MagicMock()
    modules = mock.MagicMock()
    systemd = mock.MagicMock()
    for name, module in {
        'dbus': dbus,
        'dbus.service': dbus.service,
        'dbus.mainloop': dbus.mainloop,
        'dbus.mainloop.glib': dbus.mainloop.glib,
        'gi': gi,
        'gi.repository': gi.repository,
        'host_modules': modules,
        'systemd': systemd,
        'systemd.daemon': systemd.daemon,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    signal_handler = mock.Mock()
    monkeypatch.setattr(signal, 'signal', signal_handler)
    loop = gi.repository.GObject.MainLoop.return_value
    loop.run.side_effect = lambda: signal_handler.call_args.args[1](None, None)

    def run(arguments):
        monkeypatch.setattr(sys, 'argv', [str(SERVER), *arguments])
        return runpy.run_path(str(SERVER), run_name='__main__')

    return SimpleNamespace(
        run=run, modules=modules, dbus=dbus,
        gobject=gi.repository.GObject, systemd=systemd,
    )


def assert_other_handlers_registered(environment, handlers):
    for module_name, class_name, handler_name in HANDLERS:
        constructor = getattr(getattr(environment.modules, module_name), class_name)
        constructor.assert_called_once_with(handler_name)
        assert handlers[handler_name] is constructor.return_value


def test_default_does_not_construct_debug_executor(server_environment):
    server = server_environment.run([])

    server_environment.modules.debug_service.DebugExecutor.assert_not_called()
    assert set(server['handlers']) == {entry[2] for entry in HANDLERS}
    assert_other_handlers_registered(server_environment, server['handlers'])
    assert server_environment.systemd.daemon.notify.call_args_list == [
        mock.call('READY=1'), mock.call('STOPPING=1'),
    ]


def test_explicit_opt_in_registers_debug_executor(server_environment, capsys):
    server = server_environment.run(['--enable-debug-executor'])

    constructor = server_environment.modules.debug_service.DebugExecutor
    constructor.assert_called_once_with('DebugExecutor')
    assert set(server['handlers']) == {entry[2] for entry in HANDLERS} | {
        'debug_service',
    }
    assert server['handlers']['debug_service'] is constructor.return_value
    assert_other_handlers_registered(server_environment, server['handlers'])
    assert 'WARNING: DebugExecutor enabled' in capsys.readouterr().err


@pytest.mark.parametrize('arguments', [
    ['--enable-debug-executor=false'],
    ['--enable-debug-executor', 'false'],
    ['--enable-debug'],
    ['--unknown-option'],
])
def test_invalid_arguments_fail_before_registration(server_environment, arguments):
    with pytest.raises(SystemExit) as error:
        server_environment.run(arguments)

    assert error.value.code == 2
    assert server_environment.modules.mock_calls == []
    server_environment.gobject.MainLoop.assert_not_called()
    server_environment.dbus.mainloop.glib.DBusGMainLoop.assert_not_called()
    server_environment.systemd.daemon.notify.assert_not_called()


def test_help_documents_opt_in_without_registration(server_environment, capsys):
    with pytest.raises(SystemExit) as error:
        server_environment.run(['--help'])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert '--enable-debug-executor' in output
    assert 'disabled by default' in output
    assert server_environment.modules.mock_calls == []
    server_environment.gobject.MainLoop.assert_not_called()
