from __future__ import absolute_import

import json

from dldd.filesystem import atomic_copy, atomic_write_json, load_json_object


def test_atomic_file_replacement_contract(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_text("old")
    file_syncs = []
    directory_syncs = []
    monkeypatch.setattr(
        "dldd.filesystem.os.fsync", lambda descriptor: file_syncs.append(descriptor)
    )
    monkeypatch.setattr(
        "dldd.filesystem._fsync_directory",
        lambda directory: directory_syncs.append(directory),
    )

    atomic_write_json(str(destination), {"value": 7})

    assert json.loads(destination.read_text()) == {"value": 7}
    assert len(file_syncs) == 1
    assert directory_syncs == [str(tmp_path)]
    assert not list(tmp_path.glob(".dldd-*"))

    source = tmp_path / "source.yaml"
    source.write_bytes(b"new rules")
    destination = tmp_path / "active.yaml"
    destination.write_bytes(b"old rules")
    file_syncs.clear()
    directory_syncs.clear()

    atomic_copy(str(source), str(destination))

    assert destination.read_bytes() == b"new rules"
    assert len(file_syncs) == 1
    assert directory_syncs == [str(tmp_path)]
    assert not list(tmp_path.glob(".dldd-*"))


def test_load_json_object_contract(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"ready": true}')

    assert load_json_object(str(path)) == {"ready": True}
    assert load_json_object(str(tmp_path / "missing.json")) == {}

    for contents in ("{", "[]", "null", "42"):
        path.write_text(contents)
        assert load_json_object(str(path)) == {}, "contents={!r}".format(
            contents
        )
