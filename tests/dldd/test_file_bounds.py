import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dldd import adapters
from dldd.adapters import FileAdapter, SysfsAdapter
from dldd.models import ValueConfig
from dldd.runtime import EvaluationResultType, MonitorWorkItem


FILE_BYTES = 1024 * 1024
SAMPLE_BYTES = 4 * FILE_BYTES


def _item(adapter, file_path, value_config=None, evaluation=None, **source):
    return MonitorWorkItem(
        rule_id=1000001,
        rule_name="FILE_BOUND_TEST",
        rule_version="1.0.0",
        schema_version="0.0.1",
        severity="WARNING",
        priority=1,
        symptom="SYMPTOM_OVER_THRESHOLD",
        error_type="SENSOR",
        component_type="SENSOR",
        component_name="SENSOR0",
        event_id=1,
        correlation_key="1000001:1:SENSOR0",
        source_id="SENSOR|0",
        source_type=adapter.source_type,
        source={"file": str(file_path), **source},
        evaluation=evaluation
        or {"type": "comparison", "operator": "==", "value": "ready"},
        value_config=value_config or ValueConfig(),
    )


def test_file_byte_limit_and_next_sample_recovery(tmp_path):
    path = tmp_path / "sensor"
    adapter = FileAdapter()
    item = _item(adapter, path)
    path.write_bytes(b"x" * FILE_BYTES)
    exact = adapter.collect(item)
    assert exact.result is EvaluationResultType.NO_MATCH
    assert exact.value is not None
    assert len(exact.value.raw) == FILE_BYTES

    path.write_bytes(b"x" * (FILE_BYTES + 1))
    failed = adapter.collect(item)
    assert failed.result is EvaluationResultType.COLLECTION_ERROR
    assert failed.retryable
    assert "byte limit" in failed.error
    assert failed.value is None

    path.write_text("ready", encoding="utf-8")
    assert adapter.collect(item).result is EvaluationResultType.MATCH


def test_aggregate_raw_byte_limit_and_next_sample_recovery(tmp_path):
    adapter = FileAdapter()
    item = _item(adapter, tmp_path / "sensor-*")
    for index in range(4):
        (tmp_path / "sensor-{}".format(index)).write_bytes(b"x" * FILE_BYTES)
    exact = adapter.collect(item)
    assert exact.result is EvaluationResultType.NO_MATCH
    assert exact.value is not None
    assert isinstance(exact.value.raw, list)
    assert sum(map(len, exact.value.raw)) == SAMPLE_BYTES

    extra = tmp_path / "sensor-4"
    extra.write_bytes(b"x")
    failed = adapter.collect(item)
    assert failed.result is EvaluationResultType.COLLECTION_ERROR
    assert "byte limit" in failed.error
    extra.unlink()
    assert adapter.collect(item).result is EvaluationResultType.NO_MATCH


def test_aggregate_budget_counts_json_before_path_extraction(tmp_path):
    adapter = SysfsAdapter()
    for index in range(5):
        (tmp_path / "sensor-{}".format(index)).write_text(
            " " * (900 * 1024) + '{"value": 7}', encoding="utf-8"
        )
    item = _item(
        adapter, tmp_path / "sensor-*", format="json", path="value",
        value_config=ValueConfig(type="int"),
        evaluation={"type": "comparison", "operator": "==", "value": 7},
    )
    failed = adapter.collect(item)
    assert failed.result is EvaluationResultType.COLLECTION_ERROR
    assert "byte limit" in failed.error


def test_match_limit_preserves_sorted_any_evaluation_and_recovers(tmp_path, monkeypatch):
    adapter = FileAdapter()
    for index in reversed(range(128)):
        (tmp_path / "sensor-{:03d}".format(index)).write_text(str(index))
    item = _item(
        adapter, tmp_path / "sensor-*", format="int",
        value_config=ValueConfig(type="int"),
        evaluation={"type": "comparison", "operator": ">", "value": 126},
    )
    exact = adapter.collect(item)
    assert exact.result is EvaluationResultType.MATCH
    assert exact.value is not None
    assert exact.value.raw == list(range(128))

    extra = tmp_path / "sensor-128"
    extra.write_text("128")
    with monkeypatch.context() as patch:
        patch.setattr(
            adapters.os, "open",
            lambda *args, **kwargs: pytest.fail("fanout must fail before any open"),
        )
        failed = adapter.collect(item)
    assert failed.result is EvaluationResultType.COLLECTION_ERROR
    assert "128 matched files" in failed.error

    extra.unlink()
    assert adapter.collect(item).result is EvaluationResultType.MATCH


def test_match_enumeration_stops_on_first_overflow(monkeypatch):
    consumed = []

    def matches(pattern):
        for index in range(130):
            consumed.append(index)
            if index == 129:
                pytest.fail("glob consumed beyond the first overflowing match")
            yield "sensor-{}".format(index)

    monkeypatch.setattr(adapters.glob, "iglob", matches)
    result = FileAdapter().collect(_item(FileAdapter(), "sensor-*"))
    assert result.result is EvaluationResultType.COLLECTION_ERROR
    assert "128 matched files" in result.error
    assert consumed == list(range(129))


@pytest.mark.parametrize(
    "adapter_name,symlink", [("FileAdapter", False), ("SysfsAdapter", True)]
)
def test_fifo_rejects_without_writer_then_recovers(tmp_path, adapter_name, symlink):
    target = tmp_path / "target"
    os.mkfifo(target)
    path = tmp_path / "sensor" if symlink else target
    if symlink:
        path.symlink_to(target)
    script = """
import pathlib
import sys
from types import SimpleNamespace
from dldd.adapters import AdapterError, FileAdapter, SysfsAdapter
adapter = {adapter_name}()
item = SimpleNamespace(source={{"file": sys.argv[1]}})
try:
    adapter.get_value(item)
except AdapterError as error:
    assert "regular file" in str(error), str(error)
else:
    raise AssertionError("FIFO was accepted")
target = pathlib.Path(sys.argv[2])
target.unlink()
target.write_text("ready")
assert adapter.get_value(item) == "ready"
""".format(adapter_name=adapter_name)
    subprocess.run(
        [sys.executable, "-c", script, str(path), str(target)],
        check=True, capture_output=True, timeout=3,
    )


def test_nonregular_file_target_is_a_collection_error():
    result = FileAdapter().collect(_item(FileAdapter(), "/dev/null"))
    assert result.result is EvaluationResultType.COLLECTION_ERROR
    assert "regular file" in result.error


def test_regular_symlink_and_zero_advertised_size_are_readable(
    tmp_path, monkeypatch
):
    target = tmp_path / "target"
    target.write_text("ready")
    path = tmp_path / "sensor"
    path.symlink_to(target)
    original_fstat = os.fstat

    def pseudo_stat(descriptor):
        actual = original_fstat(descriptor)
        return SimpleNamespace(st_mode=actual.st_mode, st_size=0)

    monkeypatch.setattr(adapters.os, "fstat", pseudo_stat)
    adapter = SysfsAdapter()
    assert adapter.collect(_item(adapter, path)).result is EvaluationResultType.MATCH


@pytest.mark.parametrize("prior_bytes", [0, 3 * FILE_BYTES + FILE_BYTES // 2])
def test_inflight_reads_stop_at_byte_limit_plus_one_with_short_reads(
    tmp_path, monkeypatch, prior_bytes
):
    remaining = prior_bytes
    for index in range(4):
        count = min(FILE_BYTES, remaining)
        if count:
            (tmp_path / "sensor-{}".format(index)).write_bytes(b"x" * count)
            remaining -= count
    (tmp_path / "sensor-4").write_bytes(b"x" * (4 * FILE_BYTES))
    original_read = os.read
    observed = []

    def short_read(descriptor, count):
        chunk = original_read(descriptor, min(count, 128 * 1024))
        if os.fstat(descriptor).st_size == 4 * FILE_BYTES:
            observed.append((descriptor, count, len(chunk)))
        return chunk

    monkeypatch.setattr(adapters.os, "read", short_read)
    result = FileAdapter().collect(_item(FileAdapter(), tmp_path / "sensor-*"))
    assert result.result is EvaluationResultType.COLLECTION_ERROR
    limit = min(FILE_BYTES, SAMPLE_BYTES - prior_bytes)
    assert observed[0][1] == limit + 1
    assert sum(size for unused_descriptor, unused_count, size in observed) == limit + 1
    assert all(count <= limit + 1 for unused_descriptor, count, unused_size in observed)
    with pytest.raises(OSError):
        os.fstat(observed[-1][0])


def test_encoding_uses_raw_bytes_for_limit(tmp_path):
    path = tmp_path / "sensor"
    content = "é" * (FILE_BYTES // 2)
    path.write_bytes(content.encode("utf-16-le"))
    adapter = FileAdapter()
    item = _item(adapter, path, encoding="utf-16-le")
    assert adapter.get_value(item) == content
    path.write_bytes((content + "é").encode("utf-16-le"))
    assert adapter.collect(item).result is EvaluationResultType.COLLECTION_ERROR


@pytest.mark.parametrize(
    "format_name,content,path,config,expected",
    [
        ("text", " first\r\nsecond\rthird ", None, ValueConfig(), "first\nsecond\nthird"),
        ("json", '{"rails": [{"value": 7}]}', "rails/0/value", ValueConfig(), 7),
    ],
)
def test_finite_formats_encoding_path_and_scaling(
    tmp_path, format_name, content, path, config, expected
):
    source = tmp_path / "sensor"
    source.write_bytes(content.encode("latin-1"))
    adapter = FileAdapter()
    item = _item(
        adapter, source, value_config=config, format=format_name,
        encoding="latin-1", path=path,
        evaluation={"type": "comparison", "operator": "==", "value": expected},
    )
    adapter.validate(item)
    result = adapter.collect(item)
    assert result.result is EvaluationResultType.MATCH
    assert result.value is not None
    assert result.value.normalized == expected
