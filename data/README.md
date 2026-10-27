# sonic-host-services-data
Data files required for SONiC host services

## Local image read permissions

HostService checksum and local installation accept only world-readable regular
files beneath world-searchable directories, even for root D-Bus callers.
Symlinks are checked along the original traversal; private ancestors and
extended access ACLs are denied. Missing and inaccessible sources receive the
same denial. Local installation uses a private temporary copy of the validated
file and removes it afterward. HTTP(S) installation and download behavior are
unchanged. No managed-image store, service account or companion gNMI package
is required. Download outputs retain their existing permissions; non-public
files must be made public explicitly before these read APIs can use them.

Local read resolution is bounded to raw paths shorter than 4096 bytes,
40 symlinks and 64 descriptors per ancestor stack, plus fixed target handles.
Exceeding these safety limits returns the same read denial. Ancestor
descriptors are released before hashing or copying the validated file.


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
