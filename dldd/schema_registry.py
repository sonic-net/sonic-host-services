"""Trusted, exact-version JSON Schema contracts for DLDD rules."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

import fastjsonschema


DRAFT7_URI = "http://json-schema.org/draft-07/schema#"
SCHEMA_DIRECTORY = os.path.join(os.path.dirname(__file__), "schemas")
DEFAULT_SCHEMA_FILES = {
    "0.0.1": "dld-rules-0.0.1.json",
}
DEFAULT_LAYOUT_FILES = {
    "0.0.1": "schema-layout-0.0.1.json",
}


class SchemaRegistryError(RuntimeError):
    """An installed schema contract is missing, corrupt, or unsupported."""


@dataclass(frozen=True)
class StaticSchemaIssue:
    code: str
    message: str
    path: str


def _json_path(base: str, parts: Iterable[object]) -> str:
    path = base
    for part in parts:
        if isinstance(part, int) or str(part).isdigit():
            path += "[{}]".format(part)
        else:
            path += ".{}".format(part)
    return path


def _external_references(value, path="$"):
    if isinstance(value, Mapping):
        for name, item in value.items():
            item_path = "{}.{}".format(path, name)
            if name == "$ref" and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                yield item_path, item
            yield from _external_references(item, item_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _external_references(
                item, "{}[{}]".format(path, index)
            )


def _schema_identifiers(value, path="$"):
    if isinstance(value, Mapping):
        for name, item in value.items():
            item_path = "{}.{}".format(path, name)
            if name == "$id":
                yield item_path, item
            yield from _schema_identifiers(item, item_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _schema_identifiers(
                item, "{}[{}]".format(path, index)
            )


def _reject_duplicate_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise SchemaRegistryError(
                "installed DLDD JSON contains duplicate key {!r}".format(
                    name
                )
            )
        result[name] = value
    return result


def _compile(schema: Mapping) -> Callable:
    try:
        compiled_schema = deepcopy(schema)
        # The schema ID is metadata, not a retrieval location.  Removing it
        # from the compiler input ensures even a corrupt local fragment can
        # never make the validator attempt an HTTP lookup of the root schema.
        compiled_schema.pop("$id", None)
        return fastjsonschema.compile(
            # fastjsonschema normalizes references in place while compiling.
            # Keep the trusted registry source immutable because it is also
            # used to derive the envelope and per-signature contracts below.
            compiled_schema,
            use_default=False,
            use_formats=False,
            detailed_exceptions=True,
        )
    except Exception as error:
        raise SchemaRegistryError(
            "unable to compile installed DLDD schema: {}".format(error)
        )


def _validation_issue(error, base_path: str) -> StaticSchemaIssue:
    parts = list(getattr(error, "path", ()) or ())
    if parts and parts[0] == "data":
        parts = parts[1:]
    path = _json_path(base_path, parts)
    validator = str(getattr(error, "rule", "validation") or "validation")
    if validator == "required" and isinstance(
        getattr(error, "value", None), Mapping
    ):
        missing = [
            name
            for name in (getattr(error, "rule_definition", ()) or ())
            if name not in error.value
        ]
        if missing:
            path = _json_path(path, (missing[0],))
    message = str(getattr(error, "message", error))
    name = str(getattr(error, "name", ""))
    if name and name in message:
        message = message.replace(name, path, 1)
    return StaticSchemaIssue(
        code="schema_{}".format(validator),
        message=message,
        path=path,
    )


@dataclass(frozen=True)
class SchemaContract:
    version: str
    schema_path: str
    schema_id: str
    layout_path: Optional[str]
    envelope_validator: Callable
    signature_validator: Callable

    def validate_envelope(self, document) -> Tuple[StaticSchemaIssue, ...]:
        try:
            self.envelope_validator(document)
        except fastjsonschema.JsonSchemaValueException as error:
            return (_validation_issue(error, "$"),)
        return ()

    def validate_signature(
        self, signature, index: int
    ) -> Tuple[StaticSchemaIssue, ...]:
        base_path = "$.signatures[{}]".format(index)
        try:
            self.signature_validator(signature)
        except fastjsonschema.JsonSchemaValueException as error:
            return (_validation_issue(error, base_path),)
        return ()


class SchemaRegistry:
    """Immutable registry populated only from trusted package resources."""

    def __init__(
        self,
        schema_files: Optional[Mapping[str, str]] = None,
        layout_files: Optional[Mapping[str, str]] = None,
    ) -> None:
        files = dict(
            DEFAULT_SCHEMA_FILES if schema_files is None else schema_files
        )
        layouts = dict(
            DEFAULT_LAYOUT_FILES
            if schema_files is None and layout_files is None
            else layout_files or {}
        )
        contracts: Dict[str, SchemaContract] = {}
        schema_ids = set()
        for version in sorted(files):
            contract = self._load_contract(
                version, files[version], layouts.get(version)
            )
            if contract.schema_id in schema_ids:
                raise SchemaRegistryError(
                    "duplicate installed DLDD schema ID: {}".format(
                        contract.schema_id
                    )
                )
            schema_ids.add(contract.schema_id)
            contracts[version] = contract
        if not contracts:
            raise SchemaRegistryError("no DLDD schema contracts are installed")
        self._contracts = MappingProxyType(contracts)

    @property
    def versions(self) -> Tuple[str, ...]:
        return tuple(sorted(self._contracts))

    def get(self, version: str) -> Optional[SchemaContract]:
        return self._contracts.get(version)

    def require_exact(self, version: str) -> SchemaContract:
        contract = self.get(version)
        if contract is None:
            raise SchemaRegistryError(
                "unsupported schema_version {!r}".format(version)
            )
        return contract

    def schema_path(self, version: str) -> str:
        return self.require_exact(version).schema_path

    @staticmethod
    def _load_contract(
        version: str,
        resource: str,
        layout_resource: Optional[str] = None,
    ) -> SchemaContract:
        path = (
            resource
            if os.path.isabs(resource)
            else os.path.join(SCHEMA_DIRECTORY, resource)
        )
        try:
            with open(path, "r", encoding="utf-8") as stream:
                schema = json.load(
                    stream, object_pairs_hook=_reject_duplicate_pairs
                )
        except (OSError, ValueError) as error:
            raise SchemaRegistryError(
                "unable to load installed DLDD schema {}: {}".format(
                    version, error
                )
            )
        if not isinstance(schema, Mapping):
            raise SchemaRegistryError(
                "installed DLDD schema {} must be an object".format(version)
            )
        if schema.get("$schema") != DRAFT7_URI:
            raise SchemaRegistryError(
                "DLDD schema {} does not declare supported Draft 7".format(
                    version
                )
            )
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise SchemaRegistryError(
                "DLDD schema {} has no non-empty $id".format(version)
            )
        nested_ids = tuple(
            path
            for path, unused_value in _schema_identifiers(schema)
            if path != "$.$id"
        )
        if nested_ids:
            raise SchemaRegistryError(
                "DLDD schema {} uses unsupported nested $id at {}".format(
                    version, nested_ids[0]
                )
            )
        declared = (
            schema.get("properties", {})
            .get("schema_version", {})
            .get("const")
        )
        if declared != version:
            raise SchemaRegistryError(
                "DLDD schema registry key {} does not match schema const {!r}".format(
                    version, declared
                )
            )
        external = tuple(_external_references(schema))
        if external:
            reference_path, reference = external[0]
            raise SchemaRegistryError(
                "DLDD schema {} uses non-local reference at {}: {!r}".format(
                    version, reference_path, reference
                )
            )

        layout_path = None
        if layout_resource is not None:
            layout_path = (
                layout_resource
                if os.path.isabs(layout_resource)
                else os.path.join(SCHEMA_DIRECTORY, layout_resource)
            )

        # Compile the normative complete document schema as an installation
        # integrity check. Activation intentionally uses the two shallow/deep
        # validators below so one bad signature remains a rule-level failure.
        _compile(schema)

        # Preserve every version-specific root constraint and replace only the
        # signature item with a shallow isolation envelope.  This keeps new
        # required top-level fields schema-driven without allowing one broken
        # signature to turn into a file-level failure.
        if not isinstance(
            schema.get("properties", {}).get("signatures"), Mapping
        ):
            raise SchemaRegistryError(
                "DLDD schema {} has no signatures definition".format(version)
            )
        envelope_schema = deepcopy(schema)
        signatures_schema = envelope_schema["properties"]["signatures"]
        signatures_schema["items"] = {
            "type": "object",
            "required": ["signature"],
            "properties": {"signature": {"type": "object"}},
            "additionalProperties": True,
        }
        signature_schema = {
            "$schema": DRAFT7_URI,
            "definitions": schema.get("definitions", {}),
            "$ref": "#/definitions/signature_wrapper",
        }
        return SchemaContract(
            version=version,
            schema_path=path,
            schema_id=schema_id,
            layout_path=layout_path,
            envelope_validator=_compile(envelope_schema),
            signature_validator=_compile(signature_schema),
        )


DEFAULT_SCHEMA_REGISTRY = SchemaRegistry()
