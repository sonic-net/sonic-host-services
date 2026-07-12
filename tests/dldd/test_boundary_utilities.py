from __future__ import absolute_import

import json
import os
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from dldd import filesystem as dldd_filesystem
from dldd import platform as dldd_platform
from dldd.bounded_calls import BoundedCallGate
from dldd.command_execution import build_i2c_argv, run_shell_free
from dldd.dse import DSERegistry
from dldd.filesystem import atomic_copy, atomic_write_json
from dldd.hooks import VendorHookRegistry
from dldd.platform import PlatformIdentity, detect_identity, load_extensions
from dldd.reset import clear_runtime_state
from dldd.sonic_hash import SonicHashReader, SonicHashReaderError
from dldd.validation import CompatibilityMatcher
from dldd.watcher import RulesWatcher
from tests.dldd_fakes import FakeStateDB


def test_shell_free_runner_and_i2c_argv_command_contract():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            7,
            stdout="abcdefgh",
            stderr=b"12345678",
        )

    result = run_shell_free(
        ("diagnostic", "--read"),
        timeout=4,
        max_output_bytes=4,
        runner=runner,
    )

    assert result.argv == ("diagnostic", "--read")
    assert result.returncode == 7
    assert result.stdout == b"abcd"
    assert result.stderr == b"1234"
    assert result.stdout_text() == "abcd"
    assert result.stderr_text() == "1234"
    assert calls == [
        (
            ["diagnostic", "--read"],
            {
                "shell": False,
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "timeout": 4,
            },
        )
    ]

    absent = run_shell_free(
        ["diagnostic"],
        runner=lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout=None, stderr=None
        ),
    )

    assert absent.stdout == b""
    assert absent.stderr == b""

    for argv in ("echo unsafe", b"echo unsafe", (), ("",), ("valid", 1)):
        with pytest.raises(ValueError, match="argv"):
            run_shell_free(argv)

    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_output_bytes"):
            run_shell_free(["diagnostic"], max_output_bytes=limit)


    # I2C argv construction remains shell-free and rejects unknown operations.
    read = build_i2c_argv(
        {
            "i2c_type": "get",
            "bus": "6",
            "chip_addr": "0x58",
            "command": "0x7a",
        }
    )
    write = build_i2c_argv(
        {
            "i2c_type": "set",
            "bus": "logical",
            "chip_addr": "0x58",
            "command": "0x7a",
            "value": "0x80",
            "size": "b",
            "executable": "/vendor/i2c-write",
        },
        operation="set",
        bus=7,
    )

    assert read == (
        "/usr/sbin/i2cget",
        "-f",
        "-y",
        "6",
        "0x58",
        "0x7a",
    )
    assert write == (
        "/vendor/i2c-write",
        "-f",
        "-y",
        "7",
        "0x58",
        "0x7a",
        "0x80",
        "b",
    )

    path = {"chip_addr": "0x58", "command": "0x7a"}
    for operation in (None, "read", "", 1):
        with pytest.raises(ValueError, match="get or set"):
            build_i2c_argv(path, operation=operation)


def test_bounded_call_failure_cancellation_and_capacity_contract():
    gate = BoundedCallGate(1, "dldd-test")

    failed = gate.start(
        lambda: (_ for _ in ()).throw(RuntimeError("vendor callback failed")),
        "busy",
    )

    with pytest.raises(RuntimeError, match="vendor callback failed"):
        failed.result(timeout=2)
    assert gate.start(lambda: "recovered", "busy").result(timeout=2) == (
        "recovered"
    )

    gate = BoundedCallGate(1, "dldd-test")
    entered = threading.Event()
    release = threading.Event()

    def callback():
        entered.set()
        release.wait(2)
        raise RuntimeError("late failure")

    future = gate.start(callback, "busy")
    assert entered.wait(2)
    assert future.cancel()
    release.set()
    assert future.cancelled()

    # The worker releases its slot in finally even though the cancelled Future
    # rejects the late exception result.
    for unused_attempt in range(100):
        try:
            recovered = gate.start(lambda: "available", "busy")
        except RuntimeError:
            threading.Event().wait(0.001)
            continue
        assert recovered.result(timeout=2) == "available"
        break
    else:
        pytest.fail("bounded callback did not release its capacity")


@pytest.mark.parametrize("operation", ("write", "copy"))
def test_atomic_filesystem_cleanup_closes_descriptor_when_open_fails(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "source"
    source.write_text("rules", encoding="utf-8")
    destination = tmp_path / "destination"
    closed = []
    original_close = dldd_filesystem.os.close

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(
        dldd_filesystem.os,
        "fdopen",
        lambda *unused_args, **unused_kwargs: (_ for _ in ()).throw(
            OSError("cannot open temporary stream")
        ),
    )
    monkeypatch.setattr(dldd_filesystem.os, "close", close)

    with pytest.raises(OSError, match="temporary stream"):
        if operation == "write":
            atomic_write_json(str(destination), {"ready": True})
        else:
            atomic_copy(str(source), str(destination))

    assert len(closed) == 1
    assert not destination.exists()
    assert not list(tmp_path.glob(".dldd-*"))


class RecordingConnector(object):
    def __init__(self, values=None):
        self.values = values or {}
        self.connections = []
        self.reads = []

    def connect(self, database, wait_for_init):
        self.connections.append((database, wait_for_init))

    def get_all(self, database, key):
        self.reads.append((database, key))
        return self.values.get((database, key))


def test_sonic_hash_reader_factory_dependency_and_lazy_connector_contract(
    monkeypatch,
):
    connector = RecordingConnector()
    creations = []
    reader = SonicHashReader(
        connector_factory=lambda: creations.append(True) or connector
    )

    assert reader.read("STATE_DB", "MISSING|0") == {}
    assert reader.read("STATE_DB", "MISSING|1") == {}
    assert creations == [True]
    assert connector.connections == [("STATE_DB", False)]

    connector = RecordingConnector()
    with pytest.raises(ValueError, match="either connector"):
        SonicHashReader(connector=connector, connector_factory=lambda: connector)

    reader = SonicHashReader(connector=connector)
    for database, key, expected in (
        ("", "KEY", "database"),
        (None, "KEY", "database"),
        ("STATE_DB", "", "hash key"),
        ("STATE_DB", None, "hash key"),
    ):
        with pytest.raises(ValueError, match=expected):
            reader.read(database, key)


    # The production connector is loaded lazily and reports missing runtime deps.
    monkeypatch.setitem(sys.modules, "swsscommon", None)

    with pytest.raises(SonicHashReaderError, match="swsscommon is unavailable"):
        SonicHashReader().read("STATE_DB", "KEY")

    connector = RecordingConnector(
        {("STATE_DB", "TABLE|KEY"): {"value": "42"}}
    )
    hosts = []
    package = ModuleType("swsscommon")
    package.swsscommon = SimpleNamespace(
        SonicV2Connector=lambda host: hosts.append(host) or connector
    )
    monkeypatch.setitem(sys.modules, "swsscommon", package)

    reader = SonicHashReader()

    assert reader.read("STATE_DB", "TABLE|KEY") == {"value": "42"}
    assert hosts == ["127.0.0.1"]
    assert connector.connections == [("STATE_DB", False)]


def test_platform_identity_detection_and_failure_fallback(monkeypatch):
    module = ModuleType("sonic_py_common")
    module.device_info = SimpleNamespace(
        get_platform=lambda: "x86_64-test",
        get_machine_info=lambda: {
            "onie_product_name": "PRODUCT-A",
            "onie_machine": "fallback-machine",
            "product_name": "fallback-product",
        },
        get_sonic_version_info=lambda: {"build_version": "2026.07"},
    )
    monkeypatch.setitem(sys.modules, "sonic_py_common", module)

    identity = detect_identity()

    assert identity == PlatformIdentity(
        "x86_64-test", "PRODUCT-A", "2026.07"
    )
    assert identity.generation_identity == (
        "x86_64-test|PRODUCT-A|2026.07"
    )
    assert PlatformIdentity("unknown", None, None).generation_identity == (
        "unknown||"
    )

    module.device_info = SimpleNamespace(
        get_platform=lambda: (_ for _ in ()).throw(RuntimeError("not ready"))
    )

    assert detect_identity() == PlatformIdentity("unknown", None, None)


class PermissiveMatcher(CompatibilityMatcher):
    def product_matches(self, current_product, supported_products):
        return True

    def software_matches(self, current_version, supported_versions):
        return True


def test_platform_extension_factory_result_import_and_absence_contracts(
    monkeypatch,
):
    identity = PlatformIdentity("platform", "product", "software")
    dse_registry = DSERegistry()
    hooks = VendorHookRegistry()
    matcher = PermissiveMatcher()
    calls = []

    module = SimpleNamespace(
        create_dse_registry=lambda **kwargs: (
            calls.append(("dse", kwargs)) or dse_registry
        ),
        create_vendor_hooks=lambda: calls.append(("hooks", {})) or hooks,
        create_compatibility_matcher=lambda **kwargs: (
            calls.append(("matcher", kwargs)) or matcher
        ),
    )
    monkeypatch.setattr(
        dldd_platform.importlib, "import_module", lambda unused_name: module
    )

    extensions = load_extensions(identity, "/platform/dld_dse.yaml")

    assert extensions.dse_registry is dse_registry
    assert extensions.vendor_hooks is hooks
    assert extensions.compatibility_matcher is matcher
    assert calls == [
        (
            "dse",
            {
                "dse_path": "/platform/dld_dse.yaml",
                "product_id": "product",
                "software_version": "software",
            },
        ),
        ("hooks", {}),
        (
            "matcher",
            {"product_id": "product", "software_version": "software"},
        ),
    ]

    for factory_name in (
        "create_dse_registry",
        "create_vendor_hooks",
        "create_compatibility_matcher",
    ):
        module = SimpleNamespace(**{factory_name: lambda **kwargs: object()})
        if factory_name == "create_vendor_hooks":
            module = SimpleNamespace(**{factory_name: lambda: object()})
        monkeypatch.setattr(
            dldd_platform.importlib,
            "import_module",
            lambda unused_name, result=module: result,
        )

        with pytest.raises(TypeError, match=factory_name):
            load_extensions(
                PlatformIdentity("platform", "product", "software"),
                "/dse.yaml",
            )


    # Platform absence is optional, but a nested vendor import failure is not.
    identity = PlatformIdentity("platform", "product", "software")

    def absent(unused_name):
        raise ImportError("no platform package", name="sonic_platform")

    monkeypatch.setattr(dldd_platform.importlib, "import_module", absent)
    extensions = load_extensions(identity, "/dse.yaml")
    assert isinstance(extensions.dse_registry, DSERegistry)

    def broken(unused_name):
        raise ImportError("vendor dependency missing", name="vendor_sdk")

    monkeypatch.setattr(dldd_platform.importlib, "import_module", broken)
    with pytest.raises(ImportError, match="vendor dependency missing"):
        load_extensions(identity, "/dse.yaml")


def test_reset_decodes_redis_keys_skips_directories_and_handles_absence(
    tmp_path,
):
    state_db = FakeStateDB()
    status_key = b"DLDD_RULE_STATUS|RULE_A"
    state_db.keys = lambda pattern: (
        (status_key,) if pattern.startswith("DLDD_RULE_STATUS|") else ()
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    directory = artifacts / "dldd-directory.tar.gz"
    directory.mkdir()

    result = clear_runtime_state(
        state_db,
        str(tmp_path / "missing-state.json"),
        include_artifacts=True,
        artifact_directory=str(artifacts),
    )

    assert result.redis_keys == 1
    assert result.local_state_removed is False
    assert result.artifacts == 0
    assert directory.is_dir()

    missing = clear_runtime_state(
        FakeStateDB(),
        str(tmp_path / "still-missing.json"),
        include_artifacts=True,
        artifact_directory=str(tmp_path / "missing-artifacts"),
    )
    assert missing.artifacts == 0


def test_watcher_absent_unsettled_and_restart_failure_contract(tmp_path):
    inbox = tmp_path / "inbox.yaml"
    watcher = RulesWatcher(
        str(inbox),
        str(tmp_path / "lock"),
        str(tmp_path / "state.json"),
        settle_time=30,
        clock=lambda: 100,
    )

    assert watcher.check_once() is False
    inbox.write_text("", encoding="utf-8")
    assert watcher.check_once() is False
    inbox.write_text("rules", encoding="utf-8")
    assert watcher.check_once() is False
    assert watcher.check_once() is False

    inbox = tmp_path / "restart-inbox.yaml"
    inbox.write_text("rules", encoding="utf-8")
    state = tmp_path / "restart-state.json"
    now = [100]
    failures = []

    def restart():
        failures.append(True)
        raise RuntimeError("systemd unavailable")

    watcher = RulesWatcher(
        str(inbox),
        str(tmp_path / "locks" / "watch.lock"),
        str(state),
        settle_time=0,
        restart=restart,
        clock=lambda: now[0],
    )

    assert watcher.check_once() is False
    with pytest.raises(RuntimeError, match="systemd unavailable"):
        watcher.check_once()

    document = json.loads(state.read_text(encoding="utf-8"))
    assert "last_restart_checksum" not in document
    assert document["last_restart_error"] == "systemd unavailable"

    now[0] += 1
    with pytest.raises(RuntimeError, match="systemd unavailable"):
        watcher.check_once()
    assert failures == [True, True]
