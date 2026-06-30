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
    identity: PlatformIdentity
    dse_registry: DSERegistry
    vendor_hooks: VendorHookRegistry
    compatibility_matcher: CompatibilityMatcher
    artifact_client_factory: Optional[Callable[..., Any]] = None


def detect_identity() -> PlatformIdentity:
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
        # Absence is optional.  Import failures inside the vendor module are a
        # broken platform implementation and must remain visible.
        if error.name in ("sonic_platform", "sonic_platform.dldd"):
            return PlatformExtensions(
                identity, dse_registry, vendor_hooks, compatibility_matcher
            )
        raise

    dse_factory = getattr(module, "create_dse_registry", None)
    if dse_factory is not None:
        created = dse_factory(
            dse_path=dse_path,
            product_id=identity.product_id,
            software_version=identity.software_version,
        )
        if not isinstance(created, DSERegistry):
            raise TypeError("create_dse_registry must return DSERegistry")
        dse_registry = created

    hook_factory = getattr(module, "create_vendor_hooks", None)
    if hook_factory is not None:
        created = hook_factory()
        if not isinstance(created, VendorHookRegistry):
            raise TypeError("create_vendor_hooks must return VendorHookRegistry")
        vendor_hooks = created

    compatibility_factory = getattr(
        module, "create_compatibility_matcher", None
    )
    if compatibility_factory is not None:
        created = compatibility_factory(
            product_id=identity.product_id,
            software_version=identity.software_version,
        )
        if not isinstance(created, CompatibilityMatcher):
            raise TypeError(
                "create_compatibility_matcher must return CompatibilityMatcher"
            )
        compatibility_matcher = created
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
