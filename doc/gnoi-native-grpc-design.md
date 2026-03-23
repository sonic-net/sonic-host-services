# Design: Replace `docker exec gnoi_client` with Native gRPC Calls

## TL;DR

`gnoi_shutdown_daemon` polls DPU reboot status by checking for `"reboot complete"` in `gnoi_client` stdout — but that string never appears in the output. Every DPU shutdown poll **times out unconditionally**. The fix: replace the subprocess calls with direct Python gRPC, which also eliminates the gnmi container dependency and gives us real error messages.

## The Bug

`_poll_reboot_status()` in `scripts/gnoi_shutdown_daemon.py`:

```python
if rc_s == 0 and out_s and ("reboot complete" in out_s.lower()):
    return True
```

Actual `gnoi_client -rpc RebootStatus` output:

```
System RebootStatus
{"active":false,"status":{"status":"STATUS_SUCCESS","message":"..."}}
```

The string `"reboot complete"` never appears. The poll always exhausts its timeout, then proceeds as if the DPU halted — whether it did or not.

A secondary problem: when the Reboot RPC fails, `gnoi_client` panics with a Go stack trace on stderr. The daemon calls `execute_command(..., suppress_stderr=True)`, so the error goes to `/dev/null`. The only log is `"Reboot command failed"` with zero context.

## What Changes

Replace `docker exec gnmi gnoi_client` subprocess calls with direct Python gRPC using vendored [gNOI System proto](https://github.com/openconfig/gnoi/blob/main/system/system.proto) stubs.

### New files

```
host_modules/gnoi/
├── __init__.py
├── client.py              # GnoiClient wrapper (reboot + reboot_status)
├── system_pb2.py          # vendored proto stubs
├── system_pb2_grpc.py
├── types_pb2.py
└── types_pb2_grpc.py
```

### Modified files

**`scripts/gnoi_shutdown_daemon.py`** — the two RPC call sites change:

`_send_reboot_command` becomes:
```python
def _send_reboot_command(self, dpu_name, dpu_ip, port):
    try:
        with GnoiClient(f"{dpu_ip}:{port}", timeout=REBOOT_RPC_TIMEOUT_SEC) as client:
            client.reboot(method=REBOOT_METHOD_HALT,
                          message="Triggered by SmartSwitch graceful shutdown")
        return True
    except grpc.RpcError as e:
        logger.log_error(f"{dpu_name}: gNOI Reboot failed: {e.code()} {e.details()}")
        return False
```

`_poll_reboot_status` becomes:
```python
def _poll_reboot_status(self, dpu_name, dpu_ip, port):
    deadline = time.monotonic() + _get_halt_timeout()
    with GnoiClient(f"{dpu_ip}:{port}", timeout=STATUS_RPC_TIMEOUT_SEC) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.reboot_status()
                if not resp.active:
                    return resp.status.status == system_pb2.RebootStatus.STATUS_SUCCESS
            except grpc.RpcError as e:
                logger.log_warning(f"{dpu_name}: RebootStatus poll error: {e.code()} {e.details()}")
            time.sleep(STATUS_POLL_INTERVAL_SEC)
    return False
```

`execute_command()` and `import subprocess` are removed.

**`setup.py`** — add `host_modules.gnoi` to packages list.

**`tests/gnoi_shutdown_daemon_test.py`** — mocks move from `execute_command` to `GnoiClient`.

### What stays the same

Main loop, CONFIG_DB subscription, DPU IP/port discovery, halt flag handling, threading model — all unchanged.

## Why vendor stubs instead of build-time generation?

sonic-host-services has no proto compilation infra. The gNOI System proto hasn't changed in years. We can migrate to build-time generation later if more protos are needed.

## Risks

- **grpcio/protobuf availability**: both are already in the SONiC build environment.
- **Proto drift**: pin to a specific gnoi commit; the System service is stable.
- **Insecure channel**: same trust model as today's `-notls` flag on midplane. TLS is future work.

## Appendix: gnoi_client output format

For readers who want to verify the bug claim — here's what `gnoi_client` actually does ([source](https://github.com/sonic-net/sonic-gnmi/blob/master/gnoi_client/system/reboot.go)):

- **Reboot**: prints `"System Reboot\n"` on success, `panic(err.Error())` on failure (Go stack trace to stderr).
- **RebootStatus**: prints `"System RebootStatus\n"` + `json.Marshal(resp)` on success, same panic on failure. The JSON is protobuf-serialized `RebootStatusResponse` with fields `active`, `wait`, `status.status`, `status.message`.
