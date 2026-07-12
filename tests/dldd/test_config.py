from __future__ import absolute_import

import builtins
import sys
from types import ModuleType, SimpleNamespace

import pytest

from dldd.config import ConfigDBProvider, DLDDConfig, load_vendor_defaults


def test_config_value_precedence_validation_and_vendor_defaults_contract(
    tmp_path, monkeypatch
):
    config = DLDDConfig.from_sources(
        config_db={
            "redis_monitor_polling_interval": "7",
            "file_monitor_polling_interval": "",
            "unknown": "99",
        },
        vendor_defaults={
            "redis_monitor_polling_interval": 8,
            "file_monitor_polling_interval": 9,
            "common_monitor_polling_interval": None,
        },
    )

    assert config.polling_intervals == {
        "redis": 7,
        "file": 9,
        "common": 60,
    }

    for values, reason in (
        ({"individual_max_failure_threshold": -1}, "unsigned 32-bit"),
        ({"individual_max_failure_threshold": 0x100000000}, "unsigned 32-bit"),
        ({"source_recovery_samples": 0}, "at least 1"),
        ({"rules_inbox_settle_time": 0}, "at least 1"),
    ):
        with pytest.raises(ValueError, match=reason):
            DLDDConfig.from_sources(config_db=values)


    missing = tmp_path / "missing.yaml"
    assert load_vendor_defaults(str(missing)) == {}

    path = tmp_path / "defaults.yaml"
    path.write_text(
        "ignored: 1\n"
        "dldd_config:\n"
        "  redis_monitor_polling_interval: 12\n"
    )
    assert load_vendor_defaults(str(path)) == {
        "redis_monitor_polling_interval": 12
    }

    path.write_text("dldd_config: []\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_vendor_defaults(str(path))

    path.write_text("dldd_config: {}\n")
    original_import = builtins.__import__

    def missing_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_yaml)
    with pytest.raises(RuntimeError, match="PyYAML is required"):
        load_vendor_defaults(str(path))


class RecordingConfigConnector(object):
    def __init__(self):
        self.connected = []
        self.subscriptions = []
        self.listened = False
        self.row = {"redis_monitor_polling_interval": "11"}

    def connect(self, wait_for_init):
        self.connected.append(wait_for_init)

    def get_table(self, table):
        assert table == "DLDD_CONFIG"
        return {"global": dict(self.row)}

    def subscribe(self, table, callback):
        self.subscriptions.append((table, callback))

    def listen(self):
        self.listened = True


def test_config_db_provider_contract(monkeypatch):
    connector = RecordingConfigConnector()
    provider = ConfigDBProvider(connector)
    updates = []

    assert provider.load() == {"redis_monitor_polling_interval": "11"}
    provider.listen(updates.append)
    table, callback = connector.subscriptions[0]
    assert table == "DLDD_CONFIG"
    callback(table, "other", {"ignored": "1"})
    assert updates == []

    connector.row = {
        "redis_monitor_polling_interval": "13",
        "file_monitor_polling_interval": "17",
    }
    callback(table, "global", {"redis_monitor_polling_interval": "13"})
    assert updates == [connector.row]
    assert connector.listened

    provider.reset()
    assert provider._connector is None

    connector = RecordingConfigConnector()
    swss = SimpleNamespace(ConfigDBConnector=lambda: connector)
    package = ModuleType("swsscommon")
    package.swsscommon = swss
    monkeypatch.setitem(sys.modules, "swsscommon", package)

    provider = ConfigDBProvider()
    assert provider.load() == {"redis_monitor_polling_interval": "11"}
    assert connector.connected == [True]

    original_import = builtins.__import__

    def missing_swss(name, *args, **kwargs):
        if name == "swsscommon":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_swss)
    with pytest.raises(RuntimeError, match="swsscommon is unavailable"):
        ConfigDBProvider().load()
