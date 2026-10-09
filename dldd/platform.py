"""Discovery of trusted SONiC platform DLDD extensions."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .dse import DSERegistry
from .hooks import VendorHookRegistry
from .validation import CompatibilityMatcher, ExactCompatibilityMatcher


@dataclass(frozen=True)
class PlatformIdentity:
    """Stable platform facts used for rule compatibility and generations."""

    platform: str
    product_id: Optional[str]
    software_version: Optional[str]

    @property
    def generation_identity(self) -> str:
        return "{}|{}|{}".format(
            self.platform, self.product_id or "", self.software_version or ""
        )


@dataclass(frozen=True)
class PlatformExtensions:
    """Trusted platform-provided DLDD extension contracts."""

    identity: PlatformIdentity
    dse_registry: DSERegistry
    vendor_hooks: VendorHookRegistry
    compatibility_matcher: CompatibilityMatcher
    artifact_client_factory: Optional[Callable[..., Any]] = None


def detect_identity() -> PlatformIdentity:
    """Read the current platform identity, falling back to unknown values."""

    try:
        from sonic_py_common import device_info

        platform = device_info.get_platform() or "unknown"
        metadata = device_info.get_machine_info() or {}
        product = (
            metadata.get("onie_product_name")
            or metadata.get("onie_machine")
            or metadata.get("product_name")
        )
        software = device_info.get_sonic_version_info().get("build_version")
        return PlatformIdentity(platform, product, software)
    except Exception:
        return PlatformIdentity("unknown", None, None)


def _create_optional_extension(
    module: Any,
    name: str,
    expected_type: Any,
    default: Any,
    **kwargs: Any,
) -> Any:
    """Invoke an optional trusted factory and enforce its return contract."""

    factory = getattr(module, name, None)
    if factory is None:
        return default
    created = factory(**kwargs)
    if not isinstance(created, expected_type):
        raise TypeError("{} must return {}".format(name, expected_type.__name__))
    return created


def load_extensions(identity: PlatformIdentity, dse_path: str) -> PlatformExtensions:
    """Load a fixed, trusted vendor module if the platform supplies one.

    Vendors may ship ``sonic_platform.dldd`` with the optional factories below
    for DSE resolution, runtime hooks, product/software compatibility matching,
    and artifact storage. Rules never influence the imported module name.
    """

    dse_registry = DSERegistry()
    vendor_hooks = VendorHookRegistry()
    compatibility_matcher = ExactCompatibilityMatcher()
    artifact_client_factory = None
    try:
        module = importlib.import_module("sonic_platform.dldd")
    except ImportError as error:
        # Missing modules are optional; failures inside one are not.
        if error.name in ("sonic_platform", "sonic_platform.dldd"):
            return PlatformExtensions(
                identity, dse_registry, vendor_hooks, compatibility_matcher
            )
        raise

    dse_registry = _create_optional_extension(
        module,
        "create_dse_registry",
        DSERegistry,
        dse_registry,
        dse_path=dse_path,
        product_id=identity.product_id,
        software_version=identity.software_version,
    )
    vendor_hooks = _create_optional_extension(
        module,
        "create_vendor_hooks",
        VendorHookRegistry,
        vendor_hooks,
    )
    compatibility_matcher = _create_optional_extension(
        module,
        "create_compatibility_matcher",
        CompatibilityMatcher,
        compatibility_matcher,
        product_id=identity.product_id,
        software_version=identity.software_version,
    )
    artifact_client_factory = getattr(module, "create_artifact_client", None)
    if artifact_client_factory is not None and not callable(
        artifact_client_factory
    ):
        raise TypeError("create_artifact_client must be callable")
    return PlatformExtensions(
        identity,
        dse_registry,
        vendor_hooks,
        compatibility_matcher,
        artifact_client_factory,
    )
