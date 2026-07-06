"""Immutable exact-version registry for installed DLDD rule contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, get_args

from pydantic import BaseModel, TypeAdapter

from .v0_0_1 import (
    EnvelopeV001,
    RulesDocumentV001,
    SCHEMA_VERSION,
    SignatureWrapperV001,
    signature_v001_to_domain,
)


class ContractRegistryError(RuntimeError):
    """The installed model registry is missing, corrupt, or inconsistent."""


@dataclass(frozen=True)
class RuleContract:
    version: str
    envelope: TypeAdapter
    signature: TypeAdapter
    document: TypeAdapter
    envelope_model: type[BaseModel]
    signature_model: type[BaseModel]
    document_model: type[BaseModel]
    to_domain: Callable[..., Any]

    # Explicit aliases make call sites self-documenting while retaining the
    # concise names used by the HLD and generated-schema tool.
    @property
    def envelope_adapter(self) -> TypeAdapter:
        return self.envelope

    @property
    def signature_adapter(self) -> TypeAdapter:
        return self.signature

    def validate_envelope(self, value):
        return self.envelope.validate_python(value, strict=True)

    def validate_signature(self, value):
        return self.signature.validate_python(value, strict=True)


def _literal_version(model: type[BaseModel]) -> Optional[str]:
    try:
        annotation = model.model_fields["schema_version"].annotation
    except (AttributeError, KeyError):
        return None
    values = get_args(annotation)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


class ContractRegistry:
    """Read-only registry populated exclusively by installed Python code."""

    def __init__(self, contracts: Mapping[str, RuleContract]):
        installed: Dict[str, RuleContract] = {}
        for key in contracts:
            if not isinstance(key, str) or not key:
                raise ContractRegistryError(
                    "DLDD contract registry keys must be non-empty strings"
                )
        for key in sorted(contracts):
            contract = contracts[key]
            if not isinstance(contract, RuleContract):
                raise ContractRegistryError(
                    "DLDD contract {} is not a RuleContract".format(key)
                )
            if contract.version != key:
                raise ContractRegistryError(
                    "DLDD contract key {} does not match version {}".format(
                        key, contract.version
                    )
                )
            declared = _literal_version(contract.document_model)
            if declared != key:
                raise ContractRegistryError(
                    "DLDD contract {} model declares schema_version {!r}".format(
                        key, declared
                    )
                )
            envelope_declared = _literal_version(contract.envelope_model)
            if envelope_declared != key:
                raise ContractRegistryError(
                    "DLDD contract {} envelope declares schema_version {!r}".format(
                        key, envelope_declared
                    )
                )
            bindings = (
                ("envelope", contract.envelope, contract.envelope_model),
                ("signature", contract.signature, contract.signature_model),
                ("document", contract.document, contract.document_model),
            )
            for name, adapter, model in bindings:
                if (
                    not isinstance(adapter, TypeAdapter)
                    or not isinstance(model, type)
                    or not issubclass(model, BaseModel)
                ):
                    raise ContractRegistryError(
                        "DLDD contract {} has invalid {} adapter/model binding".format(
                            key, name
                        )
                    )
                if adapter.core_schema != TypeAdapter(model).core_schema:
                    raise ContractRegistryError(
                        "DLDD contract {} {} adapter does not match its model".format(
                            key, name
                        )
                    )
            if not callable(contract.to_domain):
                raise ContractRegistryError(
                    "DLDD contract {} has no DTO-to-domain converter".format(key)
                )
            installed[key] = contract
        if not installed:
            raise ContractRegistryError("no DLDD rule contracts are installed")
        self._contracts = MappingProxyType(installed)

    @property
    def versions(self) -> Tuple[str, ...]:
        return tuple(self._contracts)

    def get(self, version: str) -> Optional[RuleContract]:
        return self._contracts.get(version)

    def require_exact(self, version: str) -> RuleContract:
        contract = self.get(version)
        if contract is None:
            raise ContractRegistryError(
                "unsupported schema_version {!r}".format(version)
            )
        return contract


_V001_CONTRACT = RuleContract(
    version=SCHEMA_VERSION,
    envelope=TypeAdapter(EnvelopeV001),
    signature=TypeAdapter(SignatureWrapperV001),
    document=TypeAdapter(RulesDocumentV001),
    envelope_model=EnvelopeV001,
    signature_model=SignatureWrapperV001,
    document_model=RulesDocumentV001,
    to_domain=signature_v001_to_domain,
)

CONTRACTS = MappingProxyType({SCHEMA_VERSION: _V001_CONTRACT})
DEFAULT_CONTRACT_REGISTRY = ContractRegistry(CONTRACTS)


__all__ = (
    "CONTRACTS",
    "DEFAULT_CONTRACT_REGISTRY",
    "ContractRegistry",
    "ContractRegistryError",
    "RuleContract",
)
