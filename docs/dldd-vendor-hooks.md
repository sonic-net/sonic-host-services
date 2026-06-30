# DLDD vendor hook contract

Platform packages can extend DLDD without modifying its engine.  The extension
module has one fixed trusted name:

```text
sonic_platform.dldd
```

It may expose any of these factories:

```python
def create_dse_registry(dse_path, product_id, software_version):
    return DSERegistry(
        hook=MyDSEHook(),
        source_types=("vendor_source",),
        action_types=("vendor_reset",),
        query_types=("vendor_dump",),
    )


def create_vendor_hooks():
    hooks = VendorHookRegistry()
    hooks.register("platform", MyPlatformHook())
    hooks.register("vendor_source", MyVendorSourceHook())
    hooks.register("vendor_reset", MyVendorActionHook())
    hooks.register("vendor_dump", MyVendorQueryHook())
    hooks.register("component_metadata", MyComponentMetadataHook())
    hooks.register("source_lifecycle", MySourceLifecycleHook())
    hooks.register("i2c", MyI2CBusResolverHook())
    return hooks


def create_compatibility_matcher(product_id, software_version):
    return MyCompatibilityMatcher()


def create_artifact_client(identity, artifact_directory, query_runner):
    return MyHealthzArtifactClient(
        identity=identity,
        directory=artifact_directory,
        query_runner=query_runner,
    )
```

`DSEHook` resolves symbolic rule references into typed `ResolvedSource`,
`ResolvedEvaluation`, and `ResolvedCommand` objects. Source and evaluation
values always use the canonical DSE reference grammar. Action and query
`command` values may instead be opaque non-empty strings, as allowed by schema
version `0.0.1`: `resolve_action()` and `resolve_query()` receive a parsed
`DSEReference` for canonical references and the unchanged `str` for opaque
commands. Vendor hooks must validate and allow-list opaque commands rather than
treating them as Python or shell expressions. Wildcard source results must
include a canonical component instance. `ResolvedCommand.executor` is a trusted
callable invoked with the immutable materialized operation mapping only after
the rules generation passes validation.

`ResolvedSource.path` and `vendor_data` should use stable declarative values.
DLDD includes the instance, concrete path, and vendor data in the source
identity so multiple operations for one event/instance remain distinct. If an
opaque object is unavoidable, include a stable primitive operation identifier
in `vendor_data`; object memory addresses are never used as identities.

`VendorHook` preflights and collects a registered vendor source and executes
registered vendor actions/queries. Override `validate_source()` to reject
platform-specific source fields before monitor threads start. Resolved source
data selects a registry key; it cannot select a module, class, or Python
expression.

`CompatibilityMatcher` lets a trusted platform define product and software
matching when exact strings are not appropriate. The default remains exact
matching, and activation fails closed if the device identity cannot be read.

`create_artifact_client` optionally replaces the built-in bounded filesystem
artifact store with a vendor Healthz/storage integration. DLDD calls it using
the stable named arguments `identity`, `artifact_directory`, and
`query_runner`. The result must implement `HealthzArtifactClient`; an invalid
factory or result publishes `BROKEN|FATAL` startup status rather than silently
falling back or starting action/monitor workers. If the factory is absent, DLDD
continues to use `FilesystemArtifactClient` under
`/var/lib/sonic/dldd/artifacts`. Vendor clients own their configured retention
and size policy, while `query_runner` preserves the validated built-in and
vendor query dispatch boundary.

The schema permits a direct `platform_api` object because the HLD leaves its
vendor fields open.  DLDD requires that object to contain a non-empty `hook`
registry key; remaining fields are passed unchanged to the trusted hook.

Hooks should:

- validate all platform-specific arguments during materialization;
- return stable component and source identifiers;
- honor declared timeouts cooperatively for in-process calls;
- serialize access when their hardware resource requires it;
- raise a descriptive exception for unavailable sources or failed actions;
- keep local actions non-disruptive to traffic, as required by the HLD.

The optional `component_metadata` hook receives a `get_serial_number`
collection request with `component_type` and `component_name`.  It may return a
serial string or `{ "serial_number": "..." }`; DLDD publishes an empty serial
when the platform has no lower-level identifier available.

The optional `source_lifecycle` hook receives an
`is_expected_maintenance` request with the materialized source identity.  A
true result (or a mapping with `graceful`/`suspended` true) moves the affected
keys to `SUSPENDED` without broken-rule accounting.  DLDD polls the hook and
resumes source sampling when maintenance ends; successful source recovery then
clears exact dependency-based stale-fault annotations.

An optional hook registered as `i2c` may override
`resolve_i2c_bus(bus, operation)` to map vendor logical bus names to the bus
identifier accepted by local i2c-tools. DLDD calls `validate_source()` during
activation and applies the same mapping to monitoring reads and direct I2C
local actions. Without this hook, the schema-provided bus is passed through.

DLDD executes CLI operations with `shell=False` and direct monitoring I2C reads
with `i2c_type: get`.  Vendors should prefer those built-in adapters unless the
hardware needs a platform-specific API or operation.
