# Design: Replace `docker exec gnoi_client` with Native gRPC Calls

## TL;DR

`gnoi_shutdown_daemon` issues gNOI RPCs by shelling out to `docker exec gnmi gnoi_client`. This introduces several layers of indirection that make failures hard to diagnose and completion detection unreliable. This document proposes replacing the subprocess path with direct Python gRPC calls using vendored proto stubs.

## 1. Limitations of the Current Approach

| Observation | Consequence |
|-------------|-------------|
| Requires NPU `gnmi` container running and healthy | DPU shutdown depends on an unrelated NPU container's availability, even though the RPC target is the DPU's own gnmi server |
| Subprocess + Docker CLI overhead per RPC | Extra process creation, Docker round-trip, stdout capture on each call |
| Output is unstructured text with a header line | Parsing is coupled to `gnoi_client`'s print format, which has no stability guarantee |
| gRPC status codes are not propagated | Caller only sees `rc != 0` — no status code, no error details |
| Errors surface as Go `panic()` stack traces on stderr | Diagnosing RPC failures requires SSH + manual docker exec |
| `suppress_stderr=True` on the Reboot call | Panic output is discarded; logs show only "command failed" |
| Completion check uses string matching | `"reboot complete" in out_s.lower()` does not match actual output format (see §2) |
| Tight coupling to CLI flag interface | `-module System -rpc Reboot -jsonin '{...}'` adds a serialization layer between caller and protobuf |
| Broader privilege surface | Shell-out through Docker CLI vs. a direct gRPC socket |

## 2. Existing Issue: RebootStatus Completion Detection

The poll loop checks:

```python
if rc_s == 0 and out_s and ("reboot complete" in out_s.lower()):
    return True
```

Actual `gnoi_client` output ([source](https://github.com/sonic-net/sonic-gnmi/blob/master/gnoi_client/system/reboot.go)):

```
System RebootStatus
{"active":false,"status":{"status":"STATUS_SUCCESS","message":"..."}}
```

`"reboot complete"` does not appear in this output → the poll always exhausts its timeout regardless of DPU state.

## 3. Proposed Change

Replace subprocess calls with a thin Python gRPC client using vendored [gNOI System proto](https://github.com/openconfig/gnoi/blob/main/system/system.proto) stubs.

**Before** (subprocess):
```
docker exec gnmi gnoi_client -target=<ip>:<port> -notls -module System -rpc Reboot -jsonin '{"method":3}'
```

**After** (direct gRPC):
```python
with GnoiClient(f"{dpu_ip}:{port}") as client:
    client.reboot(method=REBOOT_METHOD_HALT, message="graceful shutdown")
```

For RebootStatus, check the protobuf response directly instead of string matching:
```python
resp = client.reboot_status()
if not resp.active and resp.status.status == STATUS_SUCCESS:
    return True
```

### What this gives us

**Better error detection** — gRPC errors carry status codes and details natively:
```python
except grpc.RpcError as e:
    logger.log_error(f"{dpu_name}: Reboot failed: {e.code()} {e.details()}")
    # e.g. "UNAVAILABLE: connection refused" vs today's "command failed"
```

**Better testing** — mocks operate on typed protobuf objects instead of crafting subprocess stdout strings:
```python
# Today: mock must reproduce gnoi_client's exact text output
mock_execute.return_value = (0, "reboot complete", "")  # this doesn't even match reality

# After: mock returns a typed response
mock_client.reboot_status.return_value = RebootStatusResponse(
    active=False, status=RebootStatus(status=STATUS_SUCCESS))
```

**Correct completion check** — inspect `resp.active` and `resp.status.status` directly instead of string matching.

**Removes unnecessary NPU gnmi container dependency** — the current approach shells into the NPU's `gnmi` container to run `gnoi_client`, but there's no reason the NPU daemon needs the NPU gnmi container as an intermediary. The DPU's own gnmi server is the actual RPC endpoint; direct gRPC connects to it without involving the NPU container.

**Scales to future RPCs** — the same pattern extends to any gNOI or gNMI call without adding more subprocess wrappers:
```python
# Adding a new gNOI RPC is just another method on the client
class GnoiClient:
    def reboot(self, ...): ...
    def reboot_status(self, ...): ...
    def cancel_reboot(self, ...): ...   # future
    def system_time(self, ...): ...     # future

# Or a gNMI client alongside it
with GnmiClient(f"{dpu_ip}:{port}") as client:
    client.get(path="/system/state/...")
```
Each new RPC is a typed method with protobuf request/response — no new shell commands, no new output formats to parse.

## 4. Scope

### In scope
- Vendor Python gRPC stubs for `gnoi.system.System` (Reboot, RebootStatus)
- Lightweight `GnoiClient` wrapper
- Refactor the two RPC call sites in `GnoiRebootHandler`
- Update unit tests

New directory structure:
```
host_modules/gnoi/
├── __init__.py
├── client.py              # GnoiClient: reboot(), reboot_status(), context manager
├── system_pb2.py          # vendored from openconfig/gnoi system.proto
├── system_pb2_grpc.py
├── types_pb2.py           # dependency of system.proto
└── types_pb2_grpc.py
```

The daemon change is essentially replacing `execute_command(["docker", "exec", ...])` with:
```python
with GnoiClient(f"{dpu_ip}:{port}") as client:
    client.reboot(method=REBOOT_METHOD_HALT, ...)
    # ...
    resp = client.reboot_status()
```

### Out of scope
- TLS/mTLS on midplane (future work; midplane is trusted today)
- Main loop, config DB subscription, halt flag handling — unchanged
- Other gNOI services beyond System

## 5. Why Vendor Stubs?

sonic-host-services has no proto compilation infra. The gNOI System proto is stable (no changes in years). Vendoring keeps the build simple; can migrate to build-time generation later if more protos are needed.

## 6. Risks

| Risk | Mitigation |
|------|------------|
| grpcio/protobuf not in host environment | Already used by other SONiC components |
| Proto drift from upstream gnoi | Pin to a specific commit; System service is stable |
| Insecure channel on midplane | Same trust model as today's `-notls`; TLS is future work |
