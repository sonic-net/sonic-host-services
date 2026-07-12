from __future__ import absolute_import

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from dldd.rule_schema import (
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    ContractRegistryError,
)
from dldd.rule_schema import registry as schema_registry
from dldd.rule_schema.errors import append_path_component
from dldd.rule_schema.generate import main as generate_main


def test_schema_generator_writes_checks_and_reports_invalid_artifacts(
    tmp_path, monkeypatch
):
    version = DEFAULT_CONTRACT_REGISTRY.versions[0]
    output = tmp_path / "nested" / "rules.json"

    assert generate_main(
        ["--version", version, "--output", str(output)]
    ) == 0
    rendered = output.read_text(encoding="utf-8")
    assert '"x-dldd-schema-version": "{}"'.format(version) in rendered

    assert generate_main(
        ["--version", version, "--output", str(output), "--check"]
    ) == 0
    assert generate_main(
        ["--version", version, "--check", str(output)]
    ) == 0

    default_output = tmp_path / "default.json"
    monkeypatch.setattr(
        "dldd.rule_schema.generate.default_output_path",
        lambda unused_version: default_output,
    )
    assert generate_main(["--version", version]) == 0
    assert default_output.is_file()

    missing = tmp_path / "missing.json"

    with pytest.raises(SystemExit, match="unable to read generated schema"):
        generate_main(["--version", version, "--check", str(missing)])

    stale = tmp_path / "stale.json"
    stale.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="schema is out of date"):
        generate_main(["--version", version, "--check", str(stale)])


def test_contract_registry_lookup_shape_validation_and_diagnostic_paths():
    """Cover registry lookup, malformed contracts, and diagnostic key paths."""

    version = DEFAULT_CONTRACT_REGISTRY.versions[0]
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact(version)

    assert contract.envelope_adapter is contract.envelope
    assert contract.signature_adapter is contract.signature
    assert DEFAULT_CONTRACT_REGISTRY.get(version) is contract
    assert DEFAULT_CONTRACT_REGISTRY.get("not-installed") is None

    class StringVersion(BaseModel):
        schema_version: str

    assert schema_registry._literal_version(object) is None
    assert schema_registry._literal_version(StringVersion) is None

    # Registry rejects malformed keys and adapter/model shapes.
    version = DEFAULT_CONTRACT_REGISTRY.versions[0]
    installed = DEFAULT_CONTRACT_REGISTRY.require_exact(version)

    for key in (None, ""):
        with pytest.raises(ContractRegistryError, match="non-empty strings"):
            ContractRegistry({key: installed})

    with pytest.raises(ContractRegistryError, match="is not a RuleContract"):
        ContractRegistry({version: object()})

    class NoSchemaVersion(BaseModel):
        value: str

    with pytest.raises(ContractRegistryError, match="model declares"):
        ContractRegistry(
            {
                version: replace(
                    installed,
                    document=TypeAdapter(NoSchemaVersion),
                    document_model=NoSchemaVersion,
                )
            }
        )

    with pytest.raises(ContractRegistryError, match="envelope declares"):
        ContractRegistry(
            {
                version: replace(
                    installed,
                    envelope=TypeAdapter(NoSchemaVersion),
                    envelope_model=NoSchemaVersion,
                )
            }
        )

    with pytest.raises(ContractRegistryError, match="invalid envelope"):
        ContractRegistry(
            {
                version: replace(installed, envelope="not-an-adapter")
            }
        )

    # Non-string mapping keys use stable JSONPath-compatible components.
    assert append_path_component("$", True) == "$[1]"
    assert append_path_component("$", (1, 2)) == '$["(1, 2)"]'
