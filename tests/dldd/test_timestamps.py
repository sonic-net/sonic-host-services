from __future__ import absolute_import

from dldd.timestamps import floor_timestamp, floor_timestamp_fields


def test_timestamp_normalization_contract():
    assert floor_timestamp(1234.999) == 1234
    assert floor_timestamp(None) is None

    payload = {
        "timestamp": 100.9,
        "nested": {
            "started_at": 101.8,
            "hold_deadline": 102.7,
        },
        "events": [
            {
                "event_timestamp": 106.3,
                "sampling_interval": 60.25,
            }
        ],
        "last-detection-time": 107.2,
    }

    normalized = floor_timestamp_fields(payload)

    assert normalized["timestamp"] == 100
    assert normalized["nested"] == {
        "started_at": 101,
        "hold_deadline": 102,
    }
    assert normalized["events"][0]["event_timestamp"] == 106
    assert normalized["events"][0]["sampling_interval"] == 60.25
    assert normalized["last-detection-time"] == 107
