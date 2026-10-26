# sonic-host-services-data
Data files required for SONiC host services

## Managed image artifacts

The data package provisions the non-login `sonic-image` system account.
Deploy this package together with the corresponding Python host-service
modules and the gNMI image-worker update.

HostService download destinations and local install/checksum sources must be
managed image paths such as
`/host/sonic-image-exports/images/release/sonic.bin`. HTTP(S) installer URLs
remain supported; arbitrary local paths and `file://` URLs are not supported.
Artifact names use ASCII letters, digits, dots, underscores and hyphens, begin
with a letter or digit, and contain at most 128 characters per component and
16 components.

The first valid image request provisions the fixed store below a root-owned `/host` that
is not group/world writable. The export and private staging directories are owned by `sonic-image`
with mode `0700`; published regular files have mode `0600` and one link.
Existing unsafe directories are rejected, not repermissioned. Do not manually
copy files into the export: publish through HostService download or managed
gNOI Put/TransferToRemote instead.

Reads, staging and publication run as `sonic-image` with no supplementary
groups. The privileged service receives an already-open descriptor; it never
reopens a caller-selected path as root. Kernel permissions, ACLs and ancestor
traversal apply. Symlinks, hardlinks and root-owned files are not exports.
Missing and inaccessible artifacts receive the same read denial.

The services share a root-owned `0600` ownership journal and an exclusive
lock in the fixed store. If an upgrade or rollback changes the system account's
numeric UID/GID, the next image request validates the entire images/staging
trees before migrating ownership. Only private, correctly named regular files
with one link and private directories owned by the recorded identities qualify.
Symlinks, special files, unknown/root owners and unsafe modes stop migration;
administrators must investigate these entries, not recursively chown the store.
Migration changes metadata through pinned descriptors without reading contents
as root. A durable in-progress journal permits interrupted migration to resume,
including when a subsequent rollback selects another identity. Both companion
updates are required; older implementations do not understand this journal.

For local installation, the service copies the authorized descriptor into a
temporary root-only directory on `/host`, passes that fixed copy to
`sonic-installer`, and removes it after success or failure. Allow additional
disk space for one image-sized installation copy. Incomplete downloads stay
private and are removed rather than published.

This intentionally replaces arbitrary-path read compatibility with a managed
image API. Existing D-Bus caller authorization is unchanged. The root-controlled
store, account database and mount configuration remain trusted; this policy
is not a defense against a malicious local OS root.


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
