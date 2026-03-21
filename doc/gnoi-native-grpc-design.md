# Design: Replace `docker exec gnoi_client` with Native gRPC Calls

## 1. Background

The `gnoi_shutdown_daemon` on SmartSwitch NPU orchestrates graceful DPU shutdown by issuing gNOI `System.Reboot(HALT)` and polling `System.RebootStatus`. Today it does this by shelling out:

```
docker exec gnmi gnoi_client -target=<ip>:<port> -notls -module System -rpc Reboot ...
docker exec gnmi gnoi_client -target=<ip>:<port> -notls -module System -rpc RebootStatus
```

This has several problems:

| Problem | Impact |
|---------|--------|
| Requires the `gnmi` container to be running and healthy | If gnmi container is restarting or unhealthy, DPU shutdown fails silently |
| Subprocess overhead per RPC call | Extra process creation, Docker CLI round-trip, stdout parsing |
| Fragile output parsing | `"reboot complete" in out_s.lower()` breaks on any output format change |
| No structured error handling | gRPC status codes are lost; only `rc != 0` is checked |
| Error output is a Go panic stack trace | Extremely painful to diagnose failures (see §1.1) |
| Security surface | Shell-out through Docker CLI is a wider attack surface than a direct socket |

### 1.1 gnoi_client Output Format Analysis

The `gnoi_client` binary in sonic-gnmi is a Go CLI tool. Understanding its output format reveals why the current approach is fragile:

**Reboot RPC (`-rpc Reboot`):**
- On **success**: prints `"System Reboot\n"` to stdout, exits 0. No structured output.
- On **failure**: calls `panic(err.Error())`, which dumps a **Go panic stack trace** to stderr and exits with a non-zero code. The daemon only checks `rc != 0` — the actual gRPC error code, message, and details are buried in a multi-line panic dump that is not parsed.

**RebootStatus RPC (`-rpc RebootStatus`):**
- On **success**: prints `"System RebootStatus\n"` header followed by JSON-marshaled `RebootStatusResponse`, e.g.:
  ```json
  System RebootStatus
  {"active":false,"status":{"status":"STATUS_SUCCESS","message":"..."}}
  ```
- On **failure**: same `panic(err.Error())` — Go stack trace, non-zero exit.

**The parsing bug:** The daemon currently checks:
```python
if rc_s == 0 and out_s and ("reboot complete" in out_s.lower()):
    return True
```
But the actual protobuf `RebootStatusResponse` serialized to JSON contains fields like `"active":false` and `"status":"STATUS_SUCCESS"` — the string `"reboot complete"` never appears in the output. This means the poll loop **always times out** regardless of whether the DPU successfully halted, and the daemon proceeds purely on the timeout path.

**Why this matters for error diagnosis:** When a gNOI RPC fails (DPU unreachable, TLS mismatch, auth failure, server-side error), the only signal is a Go panic:
```
panic: rpc error: code = Unavailable desc = connection error: ...

goroutine 1 [running]:
main.main()
        /sonic/gnoi_client/gnoi_client.go:42
...
```
For the Reboot call, `_send_reboot_command()` invokes `execute_command(..., suppress_stderr=True)`, so this panic output on stderr is suppressed rather than logged or inspected — the daemon just logs `"Reboot command failed"` with no actionable context. Diagnosing production failures requires SSHing into the switch, manually running the docker exec command, and reading Go stack traces.

## 2. Goal

Replace the subprocess-based `gnoi_client` invocations with direct Python gRPC calls using generated protobuf stubs for the [OpenConfig gNOI System service](https://github.com/openconfig/gnoi/blob/main/system/system.proto).

## 3. Scope

### In Scope
- Generate or vendor Python gRPC stubs for `gnoi.system.System` (Reboot, RebootStatus RPCs)
- Create a lightweight `GnoiClient` wrapper class
- Refactor `GnoiRebootHandler._send_reboot_command()` and `_poll_reboot_status()` to use native gRPC
- Remove `execute_command()` helper (becomes unused)
- Update unit tests to mock at the gRPC stub level
- Add `grpcio` and `protobuf` to package dependencies

### Out of Scope
- TLS/mTLS on the midplane channel (future work; midplane is trusted today)
- Refactoring the daemon's main loop or config DB subscription logic
- Other gNOI services beyond `System`
- Changes to how DPU IP/port are discovered from CONFIG_DB

## 4. Design

### 4.1 Phase 1 — Proto Stubs

Vendor pre-generated Python stubs from the gNOI `system.proto` definition.

**Files to add:**
```
host_modules/gnoi/
├── __init__.py
├── system_pb2.py          # generated message classes
├── system_pb2_grpc.py     # generated service stubs
├── types_pb2.py           # generated types (dependency of system_pb2)
└── types_pb2_grpc.py      # generated (empty, no services in types.proto)
```

The stubs are generated from:
- https://github.com/openconfig/gnoi/blob/main/system/system.proto
- https://github.com/openconfig/gnoi/blob/main/types/types.proto (dependency)

Generation command (for reference / CI reproducibility):
```bash
python -m grpc_tools.protoc \
  -I./proto \
  --python_out=host_modules/gnoi \
  --grpc_python_out=host_modules/gnoi \
  system/system.proto types/types.proto
```

**Packaging note:** `setup.py` currently lists packages explicitly (`['host_modules', 'utils']`). The implementation must add `'host_modules.gnoi'` to the `packages` list and corresponding `package_dir` entry, otherwise the vendored stubs won't be installed and imports will fail in deployed environments.

**Why vendor instead of build-time generation?**
- sonic-host-services has no existing proto compilation infrastructure
- The gNOI System proto is stable (no changes in years)
- Keeps the build simple; can migrate to build-time generation later if more protos are needed

### 4.2 Phase 2 — GnoiClient Wrapper

A thin wrapper providing the two RPCs we need:

```python
# host_modules/gnoi/client.py

import grpc
from . import system_pb2, system_pb2_grpc

class GnoiClient:
    """Lightweight gNOI System service client for DPU communication."""

    def __init__(self, target: str, timeout: int = 30):
        """
        Args:
            target: gRPC target in "host:port" format
            timeout: Default RPC timeout in seconds
        """
        self._channel = grpc.insecure_channel(target)
        self._stub = system_pb2_grpc.SystemStub(self._channel)
        self._timeout = timeout

    def reboot(self, method: int = 3, message: str = "") -> None:
        """
        Send System.Reboot RPC.

        Args:
            method: RebootMethod enum value (3 = HALT)
            message: Human-readable reason string

        Raises:
            grpc.RpcError: on any gRPC failure
        """
        request = system_pb2.RebootRequest(
            method=method,
            message=message,
        )
        self._stub.Reboot(request, timeout=self._timeout)

    def reboot_status(self) -> system_pb2.RebootStatusResponse:
        """
        Poll System.RebootStatus RPC.

        Returns:
            RebootStatusResponse with .active and .wait fields

        Raises:
            grpc.RpcError: on any gRPC failure
        """
        request = system_pb2.RebootStatusRequest()
        return self._stub.RebootStatus(request, timeout=self._timeout)

    def close(self):
        """Close the underlying gRPC channel."""
        if self._channel:
            self._channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
```

### 4.3 Phase 3 — Refactor gnoi_shutdown_daemon

Replace the two subprocess call sites in `GnoiRebootHandler`:

#### `_send_reboot_command` (before)
```python
def _send_reboot_command(self, dpu_name, dpu_ip, port):
    reboot_cmd = ["docker", "exec", "gnmi", "gnoi_client", ...]
    rc, out, err = execute_command(reboot_cmd, ...)
    return rc == 0
```

#### `_send_reboot_command` (after)
```python
def _send_reboot_command(self, dpu_name, dpu_ip, port):
    try:
        with GnoiClient(f"{dpu_ip}:{port}", timeout=REBOOT_RPC_TIMEOUT_SEC) as client:
            client.reboot(
                method=REBOOT_METHOD_HALT,
                message="Triggered by SmartSwitch graceful shutdown"
            )
        return True
    except grpc.RpcError as e:
        logger.log_error(f"{dpu_name}: gNOI Reboot failed: {e.code()} {e.details()}")
        return False
```

#### `_poll_reboot_status` (before)
```python
def _poll_reboot_status(self, dpu_name, dpu_ip, port):
    status_cmd = ["docker", "exec", "gnmi", "gnoi_client", ...]
    while time.monotonic() < deadline:
        rc_s, out_s, _ = execute_command(status_cmd, ...)
        if rc_s == 0 and "reboot complete" in out_s.lower():
            return True
```

#### `_poll_reboot_status` (after)
```python
def _poll_reboot_status(self, dpu_name, dpu_ip, port):
    deadline = time.monotonic() + _get_halt_timeout()
    with GnoiClient(f"{dpu_ip}:{port}", timeout=STATUS_RPC_TIMEOUT_SEC) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.reboot_status()
                if not resp.active:
                    status_enum = resp.status.status
                    status_str = system_pb2.RebootStatus.Status.Name(status_enum)
                    logger.log_notice(f"{dpu_name}: RebootStatus complete: {status_str} - {resp.status.message}")
                    return status_enum == system_pb2.RebootStatus.STATUS_SUCCESS
            except grpc.RpcError as e:
                logger.log_warning(
                    f"{dpu_name}: RebootStatus poll error: code={e.code()} details={e.details()}"
                )
            time.sleep(STATUS_POLL_INTERVAL_SEC)
    return False
```

**Key improvements over the subprocess approach:**
- **Fixes the parsing bug**: checks `resp.active == False` directly instead of the broken `"reboot complete" in stdout` match that never triggers
- **Distinguishes success from failure**: inspects `resp.status.status` enum (`STATUS_SUCCESS` vs `STATUS_FAILURE` vs `STATUS_RETRIABLE_FAILURE`)
- **Actionable error logs**: gRPC errors include status code and details (e.g., `code=UNAVAILABLE details=connection refused`) instead of opaque "command failed"

#### Removals
- `execute_command()` function — no longer needed
- `import subprocess` — no longer needed

#### Additions
- `import grpc`
- `from host_modules.gnoi.client import GnoiClient`

### 4.4 Phase 4 — Update Tests

Current tests mock `execute_command` and check return codes. New tests mock at the gRPC level:

```python
@mock.patch('gnoi_shutdown_daemon.GnoiClient')
def test_send_reboot_command_success(self, MockClient):
    mock_client = MockClient.return_value.__enter__.return_value
    # reboot() returns None on success
    mock_client.reboot.return_value = None

    result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
    assert result is True
    mock_client.reboot.assert_called_once()

@mock.patch('gnoi_shutdown_daemon.GnoiClient')
def test_send_reboot_command_failure(self, MockClient):
    mock_client = MockClient.return_value.__enter__.return_value
    error = mock.create_autospec(grpc.RpcError)
    error.code.return_value = grpc.StatusCode.UNAVAILABLE
    error.details.return_value = "connection refused"
    mock_client.reboot.side_effect = error

    result = handler._send_reboot_command("DPU0", "10.0.0.1", "8080")
    assert result is False
```

### 4.5 Dependencies

| Package | Version | Notes |
|---------|---------|-------|
| `grpcio` | >=1.51.0 | Already in SONiC build environment |
| `protobuf` | >=4.21.0 | Already in SONiC build environment |

Verify these are available in the sonic-host-services build context. If not, add to `setup.py` `install_requires`.

## 5. Implementation Plan

| Phase | Description | PR |
|-------|-------------|----|
| 1 | Vendor gNOI System proto stubs | PR #1 |
| 2 | Add `GnoiClient` wrapper + unit tests | PR #1 (same) |
| 3 | Refactor `gnoi_shutdown_daemon` to use `GnoiClient` | PR #1 (same) |
| 4 | Update existing daemon tests | PR #1 (same) |

All phases can ship as a single PR since they form one atomic change — the old subprocess path is fully replaced.

## 6. Testing

- **Unit tests**: Mock gRPC stubs, verify correct protobuf messages are sent, verify error handling for various `grpc.StatusCode` values
- **Integration test**: On a SmartSwitch testbed, trigger `config chassis modules shutdown DPU0` and verify gNOI HALT is sent and RebootStatus is polled successfully via syslog
- **Regression**: Existing CI pipeline covers the daemon; updated mocks ensure no regressions

## 7. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| gRPC/protobuf not available in host environment | Verify during build; these are already used by other SONiC components |
| Proto stub drift from upstream gnoi | Pin to a specific gnoi commit; stubs are stable |
| Insecure channel on midplane | Same trust model as today's `gnoi_client -notls`; TLS is future work |

## 8. Reference Code

- **`gnoi_client` entry point:** [`sonic-gnmi/gnoi_client/gnoi_client.go`](https://github.com/sonic-net/sonic-gnmi/blob/master/gnoi_client/gnoi_client.go) — dispatches to per-module handlers; errors use `panic()` producing Go stack traces
- **Reboot/RebootStatus implementation:** [`sonic-gnmi/gnoi_client/system/reboot.go`](https://github.com/sonic-net/sonic-gnmi/blob/master/gnoi_client/system/reboot.go) — `Reboot()` prints `"System Reboot\n"` on success; `RebootStatus()` prints JSON-serialized `RebootStatusResponse` (the output the daemon tries to parse with `"reboot complete"`)
- **gNOI System proto:** [`openconfig/gnoi/system/system.proto`](https://github.com/openconfig/gnoi/blob/main/system/system.proto) — defines `RebootMethod.HALT = 3`, `RebootStatusResponse.active`, and `RebootStatus.Status` enum (`STATUS_SUCCESS = 1`)
- **gNOI types proto (dependency):** [`openconfig/gnoi/types/types.proto`](https://github.com/openconfig/gnoi/blob/main/types/types.proto)

## 9. Future Work

- **TLS support**: Add optional mTLS when midplane security is hardened
- **Build-time proto generation**: If more gNOI/gNMI services are needed, add a proto compilation step
- **Connection pooling**: Reuse gRPC channels across polls instead of creating per-call (minor optimization)
