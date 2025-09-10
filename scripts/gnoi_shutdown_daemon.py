#!/usr/bin/env python3
"""
gnoi-shutdown-daemon

Listens for CHASSIS_MODULE_TABLE state changes in STATE_DB and, when a
SmartSwitch DPU module enters a "shutdown" transition, issues a gNOI Reboot
(method HALT) toward that DPU and polls RebootStatus until complete or timeout.
"""

import json
import time
import subprocess
import socket
import os

REBOOT_RPC_TIMEOUT_SEC   = 60   # gNOI System.Reboot call timeout
STATUS_POLL_TIMEOUT_SEC  = 60   # overall time - polling RebootStatus
STATUS_POLL_INTERVAL_SEC = 5    # delay between polls
STATUS_RPC_TIMEOUT_SEC   = 10   # per RebootStatus RPC timeout

# Support both interfaces: swsssdk and swsscommon
try:
    from swsssdk import SonicV2Connector
except ImportError:
    from swsscommon.swsscommon import SonicV2Connector

from sonic_py_common import syslogger
# Centralized transition API on ModuleBase
from sonic_platform_base.module_base import ModuleBase

_v2 = None
SYSLOG_IDENTIFIER = "gnoi-shutdown-daemon"
logger = syslogger.SysLogger(SYSLOG_IDENTIFIER)

# ##########
# helper
# ##########
def is_tcp_open(host: str, port: int, timeout: float = None) -> bool:
    """Fast reachability test for <host,port>. No side effects."""
    if timeout is None:
        timeout = float(os.getenv("GNOI_DIAL_TIMEOUT", "1.0"))
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

# ##########
# DB helpers
# ##########

def _get_dbid_state(db) -> int:
    """Resolve STATE_DB numeric ID across connector implementations."""
    try:
        return db.get_dbid(db.STATE_DB)
    except Exception:
        # Default STATE_DB index in SONiC redis instances
        return 6

def _get_pubsub(db):
    """Return a pubsub object (swsssdk or raw redis client) for keyspace notifications."""
    try:
        return db.pubsub()  # swsssdk exposes pubsub()
    except AttributeError:
        client = db.get_redis_client(db.STATE_DB)
        return client.pubsub()

def _cfg_get_entry(table, key):
    """Read CONFIG_DB row via unix-socket V2 API and normalize to str."""
    global _v2
    if _v2 is None:
        from swsscommon import swsscommon
        _v2 = swsscommon.SonicV2Connector(use_unix_socket_path=True)
        _v2.connect(_v2.CONFIG_DB)
    raw = _v2.get_all(_v2.CONFIG_DB, f"{table}|{key}") or {}
    def _s(x): return x.decode("utf-8", "ignore") if isinstance(x, (bytes, bytearray)) else x
    return {_s(k): _s(v) for k, v in raw.items()}

# ############
# gNOI helpers
# ############

def execute_gnoi_command(command_args, timeout_sec=REBOOT_RPC_TIMEOUT_SEC):
    """Run gnoi_client with a timeout; return (rc, stdout, stderr)."""
    try:
        result = subprocess.run(command_args, capture_output=True, text=True, timeout=timeout_sec)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired as e:
        return -1, "", f"Command timed out after {int(e.timeout)}s."
    except Exception as e:
        return -2, "", f"Command failed: {e}"

def get_dpu_ip(dpu_name: str):
    entry = _cfg_get_entry("DHCP_SERVER_IPV4_PORT", f"bridge-midplane|{dpu_name.lower()}")
    return entry.get("ips@")

def get_gnmi_port(dpu_name: str):
    variants = [dpu_name, dpu_name.lower(), dpu_name.upper()]
    for k in variants:
        entry = _cfg_get_entry("DPU_PORT", k)
        if entry and entry.get("gnmi_port"):
            return str(entry.get("gnmi_port"))
    return "8080"

# #########
# Main loop
# #########

def main():
    # Connect for STATE_DB pubsub + reads
    db = SonicV2Connector()
    db.connect(db.STATE_DB)

    # Centralized transition reader
    module_base = ModuleBase()

    pubsub = _get_pubsub(db)
    state_dbid = _get_dbid_state(db)

    # Listen to keyspace notifications for CHASSIS_MODULE_TABLE keys
    topic = f"__keyspace@{state_dbid}__:CHASSIS_MODULE_TABLE|*"
    pubsub.psubscribe(topic)

    logger.log_info("gnoi-shutdown-daemon started and listening for shutdown events.")

    while True:
        message = pubsub.get_message()
        if message and message.get("type") == "pmessage":
            channel = message.get("channel", "")
            # channel format: "__keyspace@N__:CHASSIS_MODULE_TABLE|DPU0"
            key = channel.split(":", 1)[-1] if ":" in channel else channel

            if not key.startswith("CHASSIS_MODULE_TABLE|"):
                continue

            # Extract module name
            try:
                dpu_name = key.split("|", 1)[1]
            except IndexError:
                continue

            # Read state via centralized API
            try:
                entry = module_base.get_module_state_transition(db, dpu_name) or {}
            except Exception as e:
                logger.log_error(f"Failed reading transition state for {dpu_name}: {e}")
                time.sleep(1)
                continue

            if entry.get("state_transition_in_progress", "False") == "True" and entry.get("transition_type") == "shutdown":
                logger.log_info(f"Shutdown request detected for {dpu_name}. Initiating gNOI reboot.")
                try:
                    dpu_ip = get_dpu_ip(dpu_name)
                    port = get_gnmi_port(dpu_name)
                    if not dpu_ip:
                        raise RuntimeError("DPU IP not found")
                except Exception as e:
                    logger.log_error(f"Error getting DPU IP or port for {dpu_name}: {e}")
                    time.sleep(1)
                    continue

                # skip if TCP is not reachable
                if not is_tcp_open(dpu_ip, int(port)):
                    logger.log_info(f"Skipping {dpu_name}: {dpu_ip}:{port} unreachable (offline/down)")
                    time.sleep(1)
                    continue

                # 1) Send Reboot HALT
                logger.log_notice(f"Issuing gNOI Reboot to {dpu_ip}:{port}")
                reboot_cmd = [
                    "docker", "exec", "gnmi", "gnoi_client",
                    f"-target={dpu_ip}:{port}",
                    "-logtostderr", "-notls",
                    "-module", "System",
                    "-rpc", "Reboot",
                    "-jsonin", json.dumps({"method": 3, "message": "Triggered by SmartSwitch graceful shutdown"})
                ]
                rc, out, err = execute_gnoi_command(reboot_cmd, timeout_sec=REBOOT_RPC_TIMEOUT_SEC)
                if rc != 0:
                    logger.log_error(f"gNOI Reboot command failed for {dpu_name}: {err or out}")
                    # As per HLD, daemon just logs and returns.
                    time.sleep(1)
                    continue

                # 2) Poll RebootStatus with a real deadline
                logger.log_notice(
                    f"Polling RebootStatus for {dpu_name} at {dpu_ip}:{port} "
                    f"(timeout {STATUS_POLL_TIMEOUT_SEC}s, interval {STATUS_POLL_INTERVAL_SEC}s)"
                )
                deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SEC
                reboot_successful = False

                status_cmd = [
                    "docker", "exec", "gnmi", "gnoi_client",
                    f"-target={dpu_ip}:{port}",
                    "-logtostderr", "-notls",
                    "-module", "System",
                    "-rpc", "RebootStatus"
                ]
                while time.monotonic() < deadline:
                    rc_s, out_s, err_s = execute_gnoi_command(status_cmd, timeout_sec=STATUS_RPC_TIMEOUT_SEC)
                    if rc_s == 0 and out_s and ("reboot complete" in out_s.lower()):
                        reboot_successful = True
                        break
                    time.sleep(STATUS_POLL_INTERVAL_SEC)

                if reboot_successful:
                    logger.log_info(f"Reboot completed successfully for {dpu_name}.")
                else:
                    logger.log_warning(f"Reboot status polling timed out for {dpu_name}.")

                # NOTE:
                # Do NOT clear CHASSIS_MODULE_TABLE transition flags here.
                # Per HLD and platform flow, the transition is cleared by the
                # platform's module.py AFTER set_admin_state(down) has completed
                # (i.e., after the module is actually taken down). This avoids
                # prematurely unblocking other components before shutdown finishes.

        time.sleep(1)

if __name__ == "__main__":
    main()
