from __future__ import absolute_import

import pytest

from dldd.config import ConfigDBProvider, DLDDConfig, load_vendor_defaults


def test_config_value_precedence_validation_and_vendor_defaults_contract(tmp_path):
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
        ({"rules_inbox_settle_time": 0}, "at least 1"),
    ):
        with pytest.raises(ValueError, match=reason):
            DLDDConfig.from_sources(config_db=values)

    path = tmp_path / "defaults.yaml"
    path.write_text(
        "ignored: 1\n"
        "dldd_config:\n"
        "  redis_monitor_polling_interval: 12\n"
    )
    assert load_vendor_defaults(str(path)) == {
        "redis_monitor_polling_interval": 12
    }


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


def test_config_db_provider_contract():
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
