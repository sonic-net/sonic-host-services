# sonic-host-services-data
Data files required for SONiC host services

## DebugExecutor is disabled by default

HostService does not register the DebugExecutor D-Bus object unless
`sonic-host-server` is started with `--enable-debug-executor`. With the flag
absent, `RunCommand` and its output signals are unavailable; other HostService
handlers remain enabled. The packaged systemd unit does not pass this flag,
so DebugExecutor is disabled by default, including in production.

DebugExecutor permits caller-selected commands to run with HostService's
privileges. Enabling it does not add per-caller authorization or command
validation. Production startup should leave it disabled. Only an administrator
should opt in for an approved debugging workflow, after reviewing the callers
that can access the host system bus.

For an explicit opt-in, an administrator can use a root-managed drop-in for
`sonic-hostservice.service` to replace its `ExecStart`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -u /usr/local/bin/sonic-host-server --enable-debug-executor
```

Reload systemd configuration and perform a controlled restart of
`sonic-hostservice.service` to apply the change. To disable it again, remove
the opt-in override (preserving any unrelated service overrides), reload and
restart. The flag is a HostService startup setting, not a telemetry/gNMI flag
or a `RunCommand` argument; clients cannot enable it through a D-Bus request.
Existing clients that require DebugExecutor must arrange explicit opt-in
with their platform administrator before upgrading. No method or signal
signature changes when it is enabled.

This default-off gate reduces the exposed command-execution surface. It does
not implement per-service authorization for other HostService methods or
change their configuration and filesystem validation.

## Checksum read permissions

HostService checksum accepts only world-readable regular
files beneath world-searchable directories, even for root D-Bus callers.
Symlinks are checked along the original traversal; private ancestors and
extended access ACLs are denied. Missing and inaccessible sources receive the
same denial. No staging copy, managed-image store, service account or
companion gNMI package is required. Existing non-public files are not changed
and must be made public explicitly before checksum can read them.

Local read resolution is bounded to raw paths shorter than 4096 bytes,
40 symlinks and 64 descriptors per ancestor stack, plus fixed target handles.
Exceeding these safety limits returns the same read denial. Ancestor
descriptors are released before hashing the validated file.


## To build

```
dpkg-buildpackage -rfakeroot -b -us -uc
```

## To clean

```
dpkg-buildpackage -rfakeroot -Tclean
```

---

See the [SONiC Website](https://sonicfoundation.dev/) for more information about the SONiC project.
