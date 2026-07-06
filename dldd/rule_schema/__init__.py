"""Versioned Pydantic contracts for DLDD rule sources.

The models in this package are the wire-contract boundary.  Runtime code should
convert successful validation DTOs to the recursively immutable domain models
in :mod:`dldd.models` before retaining or executing them.
"""

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
    "DomainConversionError",
    "ContractRegistry",
    "ContractRegistryError",
    "RuleContract",
    "normalize_validation_error",
)
