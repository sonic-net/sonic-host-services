"""Versioned Pydantic contracts for DLDD rule sources."""

from .errors import (
    ContractIssue,
    DomainConversionError,
    normalize_validation_error,
)
from .registry import (
    CONTRACTS,
    DEFAULT_CONTRACT_REGISTRY,
    ContractRegistry,
    ContractRegistryError,
    RuleContract,
)

__all__ = (
    "CONTRACTS",
    "DEFAULT_CONTRACT_REGISTRY",
    "ContractIssue",
    "ContractRegistry",
    "ContractRegistryError",
    "DomainConversionError",
    "RuleContract",
    "normalize_validation_error",
)
