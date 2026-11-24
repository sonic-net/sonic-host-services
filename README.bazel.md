The native Bazel entry points mirror the original Python setup.py, Rust Cargo
release profile and data/debian/rules. Runtime archives are uncompressed.

| Producer | Public targets |
| --- | --- |
| Python 1.0 | `//:sonic-host-services.runtime`, `//:sonic-host-services.runtime_metadata` |
| Data 1.0.0 | `//:sonic-host-services-data.runtime`, `//:sonic-host-services-data.runtime_metadata` |
| Rust 1.0.0 | `//:sonic-host-services-rs.runtime`, `//:sonic-host-services-rs.runtime.debug_symbols`, `//:sonic-host-services-rs.runtime_metadata` |

The default `//:enable_host_service_on_start` matches Make's enabled setting.
Setting it to false changes only sonic-hostservice automatic enablement; both
settings preserve all units and the original no-start behavior.

Python scripts retain their original bodies and install as executable files with
`/usr/bin/python3`, matching the original Python package installer. Metadata keeps
the original requirements and testing extra. No intermediate wheel is created.

The original Cargo.toml and Cargo.lock remain unchanged for Cargo builds. Bazel
uses the shared sonic-rust-deps graph and its generated lock. The public Common
Rust target follows buildimage's Make-selected source revision 334389eae54a;
Common owns its generated bindings and shared C++ runtime. The shared release
transition applies Cargo's opt-level 3 and codegen-units 16 throughout the
dependency graph. The FFI test checks the Common C ABI and shared Serde identity.

Run `bazel test //:installation_test //crates/procdockerstatsd-rs:locked_ffi_test`
and build the three metadata targets above. The original Azure pytest pipeline
remains the source Python test suite.

Formal consumption uses sonic-build-infra
`0.0.44-35726399908a3beb947174e9359f902077928289`, Common
`0.0.8-2a4ddd070edb719f2e969a880a5cfff711e64e72`, and sonic-rust-deps
`0.0.5-5bf69bc0a7d9ea9f4a5606264be592867cd29d1e`.
Normal registry consumption selects these public sources without a local Common
or infrastructure override. Hosted CI remains a separate publication gate.
