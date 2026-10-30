# sonic-host-services-data
Data files required for SONiC host services

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
