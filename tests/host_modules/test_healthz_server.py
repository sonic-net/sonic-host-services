"""Host server registration behavior with and without Healthz."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _registration_namespace(healthz_constructor):
    # Importing the script would start its D-Bus main loop. Extract only the
    # registration function so this contract can be checked without D-Bus.
    script = Path(__file__).resolve().parents[2] / "scripts" / "sonic-host-server"
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    register = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "register_dbus"
    )

    importer = mock.Mock(return_value=SimpleNamespace(Healthz=healthz_constructor))
    namespace = {"handlers": {}, "logger": mock.Mock(),
                 "importlib": SimpleNamespace(import_module=importer)}
    existing = {
        "config_engine": ("Config", "config"),
        "gcu": ("GCU", "gcu"),
        "host_service": ("HostService", "host_service"),
        "reboot": ("Reboot", "reboot"),
        "showtech": ("Showtech", "showtech"),
        "systemd_service": ("SystemdService", "systemd"),
        "image_service": ("ImageService", "image_service"),
        "docker_service": ("DockerService", "docker_service"),
        "file_service": ("FileService", "file_stat"),
        "debug_service": ("DebugExecutor", "debug_service"),
        "debug_info": ("DebugArtifactCollector", "debug_info"),
        "gnoi_reset": ("GnoiReset", "gnoi_reset"),
        "ssh_mgmt": ("SshMgmt", "ssh_mgmt"),
        "gnsi_console": ("GnsiConsole", "gnsi_console"),
        "glome": ("Glome", "glome"),
    }
    expected = {}
    for module_name, (class_name, handler_name) in existing.items():
        instance = object()
        constructor = mock.Mock(return_value=instance)
        namespace[module_name] = SimpleNamespace(**{class_name: constructor})
        expected[handler_name] = instance
    module = ast.Module(body=[register], type_ignores=[])
    exec(compile(module, str(script), "exec"), namespace)  # noqa: S102 - isolated function AST
    return namespace, expected


def test_healthz_init_failure_keeps_existing_dbus_handlers():
    healthz_constructor = mock.Mock(side_effect=RuntimeError("catalog unavailable"))
    namespace, expected = _registration_namespace(healthz_constructor)

    namespace["register_dbus"]()

    assert namespace["handlers"] == expected
    namespace["importlib"].import_module.assert_called_once_with("host_modules.healthz")
    healthz_constructor.assert_called_once_with("healthz")
    namespace["logger"].exception.assert_called_once()


def test_healthz_registers_alongside_existing_handlers():
    healthz_handler = object()
    healthz_constructor = mock.Mock(return_value=healthz_handler)
    namespace, expected = _registration_namespace(healthz_constructor)

    namespace["register_dbus"]()

    assert namespace["handlers"] == {**expected, "healthz": healthz_handler}
    namespace["importlib"].import_module.assert_called_once_with("host_modules.healthz")
    healthz_constructor.assert_called_once_with("healthz")
    namespace["logger"].exception.assert_not_called()


def test_healthz_import_failure_keeps_existing_dbus_handlers():
    namespace, expected = _registration_namespace(mock.Mock())
    namespace["importlib"].import_module.side_effect = ImportError("missing module")

    namespace["register_dbus"]()

    assert namespace["handlers"] == expected
    namespace["logger"].exception.assert_called_once()
