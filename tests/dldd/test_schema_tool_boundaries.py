from __future__ import absolute_import

from dataclasses import replace

import pytest

from dldd.rule_schema import (
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    ContractRegistryError,
)
from dldd.rule_schema.generate import main as generate_main
from tests.dldd_fakes import load_valid_rules_document


def test_schema_generator_writes_and_checks_the_installed_contract(tmp_path):
    version = DEFAULT_CONTRACT_REGISTRY.versions[0]
    output = tmp_path / "nested" / "rules.json"

    assert generate_main(
        ["--version", version, "--output", str(output)]
    ) == 0
    assert '"x-dldd-schema-version": "{}"'.format(
        version
    ) in output.read_text(encoding="utf-8")
    assert generate_main(
        ["--version", version, "--output", str(output), "--check"]
    ) == 0

    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="schema is out of date"):
        generate_main(
            ["--version", version, "--output", str(output), "--check"]
        )


def test_contract_registry_exact_lookup_and_domain_conversion():
    version = DEFAULT_CONTRACT_REGISTRY.versions[0]
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact(version)

    assert DEFAULT_CONTRACT_REGISTRY.get(version) is contract
    assert DEFAULT_CONTRACT_REGISTRY.get("not-installed") is None
    with pytest.raises(ContractRegistryError, match="unsupported schema_version"):
        DEFAULT_CONTRACT_REGISTRY.require_exact("not-installed")

    document = load_valid_rules_document()
    dto = contract.validate_signature(document["signatures"][0])
    domain = contract.to_domain(
        dto,
        local_action_default_timeout=document["local_action_default_timeout"],
    )
    assert domain.schema_version == version

    with pytest.raises(ContractRegistryError, match="no DLDD rule contracts"):
        ContractRegistry({})
    with pytest.raises(ContractRegistryError, match="no DTO-to-domain converter"):
        ContractRegistry({version: replace(contract, to_domain=None)})
