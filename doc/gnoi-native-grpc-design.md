# Design: Replace `docker exec gnoi_client` with Native gRPC Calls

## TL;DR

`gnoi_shutdown_daemon` issues gNOI RPCs by shelling out to `docker exec gnmi gnoi_client`. This is fragile, opaque, and already causing silent failures. Replace with direct Python gRPC calls using vendored proto stubs.

## 1. Why This Pattern Is Bad

| Problem | Impact |
|---------|--------|
| Requires `gnmi` container running and healthy | DPU shutdown silently fails if gnmi is restarting |
| Subprocess + Docker CLI overhead per RPC | Extra process creation, Docker round-trip, stdout capture |
| Output is unstructured text with a header line | Any format change in `gnoi_client` breaks parsing |
| gRPC status codes are lost | Caller only sees `rc != 0` — no code, no details |
| Errors are Go `panic()` stack traces on stderr | Production diagnosis requires SSH + manual docker exec |
| `suppress_stderr=True` discards those panics | Error output goes to `/dev/null`, logs say only "command failed" |
| String matching for completion detection | `"reboot complete" in out_s.lower()` doesn't match actual output (see §2) |
| Tight coupling to CLI flag interface | `-module System -rpc Reboot -jsonin '{...}'` is a serialization layer we don't need |
| Security surface | Shell-out through Docker CLI is wider than a direct gRPC socket |

## 2. Already Broken: RebootStatus Parsing

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

`"reboot complete"` never appears → poll **always times out** regardless of DPU state.

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
- **Structured errors**: `grpc.RpcError` with status code + details instead of opaque exit codes
- **Correct completion check**: inspect `resp.active` and `resp.status.status` directly
- **No container dependency**: gRPC goes straight to the DPU, gnmi container health is irrelevant
- **No parsing**: protobuf deserialization, not string matching on CLI output

## 4. Scope

### In scope
- Vendor Python gRPC stubs for `gnoi.system.System` (Reboot, RebootStatus)
- Lightweight `GnoiClient` wrapper
- Refactor the two RPC call sites in `GnoiRebootHandler`
- Update unit tests

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
