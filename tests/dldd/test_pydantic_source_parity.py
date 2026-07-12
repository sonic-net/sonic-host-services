from __future__ import absolute_import

from copy import deepcopy

import pytest

from dldd.validation import validate_document
from tests.dldd_fakes import (
    load_valid_rules_document as _document,
    valid_rule_event as _event,
)


EVENT_PATH = "$.signatures[0].signature.conditions.events[0].event"


def _replace_event(event_type, path):
    def mutate(event):
        event.update(type=event_type, path=deepcopy(path))

    return mutate


def _redis_path_with_empty(field):
    path = {
        "database": "STATE_DB",
        "table": "TABLE",
        "key": "KEY",
        "path": "value",
    }
    path[field] = ""
    return path


@pytest.mark.parametrize(
    "mutation, expected_code, expected_path",
    (
        (
            _replace_event("redis", "STATE_DB:TABLE|KEY"),
            "invalid_type",
            EVENT_PATH + ".path",
        ),
        (
            _replace_event(
                "redis",
                {"database": "", "table": "TABLE", "key": "KEY", "path": "value"},
            ),
            "invalid_length",
            EVENT_PATH + ".path.database",
        ),
        (
            _replace_event("dse", "sensor:missing_parentheses"),
            "invalid_format",
            EVENT_PATH + ".path",
        ),
        (
            _replace_event("cli", {"argv": []}),
            "invalid_length",
            EVENT_PATH + ".path.argv",
        ),
        (
            _replace_event("cli", {"argv": ["/bin/true"], "timeout": 0}),
            "out_of_range",
            EVENT_PATH + ".path.timeout",
        ),
        (
            _replace_event(
                "file", {"file": "/tmp/value", "format": "text", "scaling": "2"}
            ),
            "invalid_type",
            EVENT_PATH + ".path.scaling",
        ),
        (
            _replace_event(
                "sysfs", {"file": "/sys/value", "format": "text", "unit": 1}
            ),
            "invalid_type",
            EVENT_PATH + ".path.unit",
        ),
        (
            _replace_event("platform_api", {}),
            "missing_field",
            EVENT_PATH + ".path.hook",
        ),
        (
            _replace_event("not-installed", {}),
            "unsupported_type",
            EVENT_PATH,
        ),
        (
            _replace_event(
                "i2c",
                {
                    "bus": [],
                    "chip_addr": "0x58",
                    "i2c_type": "get",
                    "command": "0x7A",
                    "size": "b",
                },
            ),
            "invalid_length",
            EVENT_PATH + ".path.bus",
        ),
        (
            _replace_event(
                "i2c",
                {
                    "bus": "1",
                    "chip_addr": "58",
                    "i2c_type": "get",
                    "command": "0x7A",
                    "size": "b",
                },
            ),
            "invalid_format",
            EVENT_PATH + ".path.chip_addr",
        ),
        (
            _replace_event(
                "i2c",
                {
                    "bus": "1",
                    "chip_addr": "0x58",
                    "i2c_type": "set",
                    "command": "0x7A",
                    "size": "b",
                },
            ),
            "unsupported_value",
            EVENT_PATH + ".path.i2c_type",
        ),
        (
            _replace_event(
                "i2c",
                {
                    "bus": "1",
                    "chip_addr": "0x58",
                    "i2c_type": "get",
                    "command": "0x7A",
                    "size": "q",
                },
            ),
            "unsupported_value",
            EVENT_PATH + ".path.size",
        ),
    ),
)
def test_versioned_pydantic_contract_owns_removed_source_invariants(
    mutation, expected_code, expected_path
):
    document = _document()
    mutation(_event(document))

    result = validate_document(document, materialize=False)

    assert result.file_valid
    assert result.materialized_rules == ()
    assert (expected_code, expected_path) in {
        (issue.code, issue.path) for issue in result.broken_rules[0].issues
    }


@pytest.mark.parametrize(
    "event_type, path, expected_path",
    (
        *(
            (
                "redis",
                _redis_path_with_empty(field),
                EVENT_PATH + ".path." + field,
            )
            for field in ("database", "table", "key", "path")
        ),
        (
            "i2c",
            {
                "bus": [],
                "chip_addr": "0x58",
                "i2c_type": "get",
                "command": "0x7A",
                "size": "b",
            },
            EVENT_PATH + ".path.bus",
        ),
        (
            "file",
            {"file": "/tmp/value", "format": "text", "scaling": "invalid"},
            EVENT_PATH + ".path.scaling",
        ),
        (
            "platform_api",
            {"hook": ""},
            EVENT_PATH + ".path.hook",
        ),
    ),
)
def test_union_branch_names_never_leak_into_operator_facing_paths(
    event_type, path, expected_path
):
    document = _document()
    _event(document).update(type=event_type, path=path)

    result = validate_document(document, materialize=False)
    issues = result.broken_rules[0].issues

    assert any(issue.path == expected_path for issue in issues)
    for marker in ("PlatformAPIHookPathV001", "constrained-", "list["):
        assert all(marker not in issue.path for issue in issues)
