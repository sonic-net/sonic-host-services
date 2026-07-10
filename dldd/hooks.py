"""Explicit extension points for vendor-owned platform behavior."""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock
from typing import Any, Dict, Mapping, Optional


class VendorHookError(RuntimeError):
    """Base error raised by a vendor hook."""


class VendorHook(ABC):
    """Vendor implementation selected by a declarative hook name.

    DLDD never imports a class named by a rules file and never evaluates Python
    expressions from rules or DSE data.  Platform packages register trusted hook
    instances at daemon startup.
    """

    @abstractmethod
    def collect(self, operation: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def execute_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError

    def validate_source(self, operation: Mapping[str, Any]) -> None:
        """Validate a source binding without touching live hardware."""

        return None

    def collect_query(self, query: Mapping[str, Any]) -> Any:
        return self.collect(query)

    def resolve_i2c_bus(
        self, bus: Any, operation: Mapping[str, Any]
    ) -> Any:
        """Map a vendor logical bus identifier to an i2c-tools bus."""

        return bus


class VendorHookRegistry:
    """Small thread-safe registry populated by trusted platform code."""

    def __init__(self) -> None:
        self._hooks: Dict[str, VendorHook] = {}
        self._lock = RLock()

    def register(self, name: str, hook: VendorHook) -> None:
        if not name or not isinstance(hook, VendorHook):
            raise ValueError("a hook name and VendorHook instance are required")
        with self._lock:
            if name in self._hooks:
                raise ValueError("vendor hook already registered: {}".format(name))
            self._hooks[name] = hook

    def get(self, name: str) -> VendorHook:
        with self._lock:
            try:
                return self._hooks[name]
            except KeyError:
                raise VendorHookError("vendor hook is not registered: {}".format(name))

    def get_optional(self, name: str) -> Optional[VendorHook]:
        with self._lock:
            return self._hooks.get(name)

    def resolve_i2c_bus(
        self, bus: Any, operation: Mapping[str, Any]
    ) -> Any:
        hook = self.get_optional("i2c")
        if hook is None:
            return bus
        resolved = hook.resolve_i2c_bus(bus, operation)
        if not (
            (isinstance(resolved, str) and resolved)
            or (isinstance(resolved, int) and not isinstance(resolved, bool))
        ):
            raise VendorHookError(
                "i2c bus resolver must return a non-empty string or integer"
            )
        return resolved

    def validate_i2c_source(self, operation: Mapping[str, Any]) -> None:
        hook = self.get_optional("i2c")
        if hook is None:
            return
        hook.validate_source(operation)
        configured = operation.get("bus")
        buses = configured if isinstance(configured, (list, tuple)) else (configured,)
        for bus in buses:
            self.resolve_i2c_bus(bus, operation)
