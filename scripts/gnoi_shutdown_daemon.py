#!/usr/bin/env python3
"""
gnoi-shutdown-daemon

Listens for CHASSIS_MODULE_TABLE state changes in STATE_DB and, when a
SmartSwitch DPU module enters a "shutdown" transition, issues a gNOI Reboot
(method HALT) toward that DPU and polls RebootStatus until complete or timeout.

Additionally, a lightweight background thread periodically enforces timeout
clearing of stuck transitions (startup/shutdown/reboot) using ModuleBase’s
common APIs, so all code paths (CLI, chassisd, platform, gNOI) benefit.
"""

import json
import time
import subprocess
import socket
import os
import sonic_py_common.daemon_base as daemon_base

REBOOT_RPC_TIMEOUT_SEC   = 60   # gNOI System.Reboot call timeout
STATUS_POLL_TIMEOUT_SEC  = 60   # overall time - polling RebootStatus
STATUS_POLL_INTERVAL_SEC = 5    # delay between polls
STATUS_RPC_TIMEOUT_SEC   = 10   # per RebootStatus RPC timeout
REBOOT_METHOD_HALT = 3          # gNOI System.Reboot method: HALT
STATE_DB_INDEX = 6

from sonic_py_common import syslogger
# Centralized transition API on ModuleBase
from sonic_platform_base.module_base import ModuleBase

SYSLOG_IDENTIFIER = "gnoi-shutdown-daemon"
logger = syslogger.SysLogger(SYSLOG_IDENTIFIER)

# ##########
# Helpers
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

def _get_pubsub(db):
    """Return a pubsub object for keyspace notifications.

    Prefer a direct pubsub() if the connector exposes one; otherwise,
    fall back to the raw redis client's pubsub().
    """
    try:
        return db.pubsub()  # some connectors expose pubsub()
    except AttributeError:
        client = db.get_redis_client(db.STATE_DB)
        return client.pubsub()

def execute_gnoi_command(command_args, timeout_sec=REBOOT_RPC_TIMEOUT_SEC):
    """Run gnoi_client with a timeout; return (rc, stdout, stderr)."""
    try:
        result = subprocess.run(command_args, capture_output=True, text=True, timeout=timeout_sec)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired as e:
        return -1, "", f"Command timed out after {int(e.timeout)}s."
    except Exception as e:
        return -2, "", f"Command failed: {e}"

def get_dpu_ip(config_db, dpu_name: str) -> str:
    key = f"bridge-midplane|{dpu_name.lower()}"
    entry = config_db.get_entry("DHCP_SERVER_IPV4_PORT", key)
    return entry.get("ips@") if entry else None

def get_dpu_gnmi_port(config_db, dpu_name: str) -> str:
    variants = [dpu_name, dpu_name.lower(), dpu_name.upper()]
    for k in variants:
        entry = config_db.get_entry("DPU_PORT", k)
        if entry and entry.get("gnmi_port"):
            return str(entry.get("gnmi_port"))
    return "8080"

# ###############
# gNOI Reboot Handler
# ###############
class GnoiRebootHandler:
    """
    Handles gNOI reboot operations for DPU modules, including sending reboot commands
    and polling for status completion.
    """
    def __init__(self, db, config_db, module_base: ModuleBase):
        self._db = db
        self._config_db = config_db
        self._mb = module_base

    def handle_transition(self, dpu_name: str, transition_type: str) -> bool:
        """
        Handle a shutdown or reboot transition for a DPU module.
        Returns True if the operation completed successfully, False otherwise.
        """
        # Set gnoi_shutdown_complete flag to False at the beginning
        self._set_gnoi_shutdown_complete_flag(dpu_name, False)

        try:
            dpu_ip = get_dpu_ip(self._config_db, dpu_name)
            port = get_dpu_gnmi_port(self._config_db, dpu_name)
            if not dpu_ip:
                raise RuntimeError("DPU IP not found")
        except Exception as e:
            logger.log_error(f"Error getting DPU IP or port for {dpu_name}: {e}")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        # skip if TCP is not reachable
        if not is_tcp_open(dpu_ip, int(port)):
            logger.log_info(f"Skipping {dpu_name}: {dpu_ip}:{port} unreachable (offline/down)")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        # Wait for gnoi halt in progress to be set by module_base
        if not self._wait_for_gnoi_halt_in_progress(dpu_name):
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        # Send Reboot HALT
        if not self._send_reboot_command(dpu_name, dpu_ip, port):
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        # Poll RebootStatus
        reboot_successful = self._poll_reboot_status(dpu_name, dpu_ip, port)

        if reboot_successful:
            logger.log_info(f"Halting the services on DPU is successful for {dpu_name}.")
        else:
            logger.log_warning(f"Status polling of halting the services on DPU timed out for {dpu_name}.")

        # clear gnoi halt in progress
        self._mb._clear_module_gnoi_halt_in_progress(dpu_name)

        # Set gnoi_shutdown_complete flag based on the outcome
        self._set_gnoi_shutdown_complete_flag(dpu_name, reboot_successful)

        return reboot_successful

    def _wait_for_gnoi_halt_in_progress(self, dpu_name: str) -> bool:
        """Poll for gnoi_halt_in_progress flag."""
        logger.log_notice(f"Waiting for gnoi halt in progress for {dpu_name}")
        deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._mb._get_module_gnoi_halt_in_progress(dpu_name):
                logger.log_info(f"gNOI halt in progress for {dpu_name}")
                return True
            time.sleep(STATUS_POLL_INTERVAL_SEC)
        logger.log_warning(f"Timed out waiting for gnoi halt in progress for {dpu_name}")
        return False

    def _send_reboot_command(self, dpu_name: str, dpu_ip: str, port: str) -> bool:
        """Send gNOI Reboot HALT command to the DPU."""
        logger.log_notice(f"Issuing gNOI Reboot to {dpu_ip}:{port}")
        reboot_cmd = [
            "docker", "exec", "gnmi", "gnoi_client",
            f"-target={dpu_ip}:{port}",
            "-logtostderr", "-notls",
            "-module", "System",
            "-rpc", "Reboot",
            "-jsonin", json.dumps({"method": REBOOT_METHOD_HALT, "message": "Triggered by SmartSwitch graceful shutdown"})
        ]
        rc, out, err = execute_gnoi_command(reboot_cmd, timeout_sec=REBOOT_RPC_TIMEOUT_SEC)
        if rc != 0:
            logger.log_error(f"gNOI Reboot command failed for {dpu_name}: {err or out}")
            return False
        return True

    def _poll_reboot_status(self, dpu_name: str, dpu_ip: str, port: str) -> bool:
        """Poll RebootStatus until completion or timeout."""
        logger.log_notice(
            f"Polling RebootStatus for {dpu_name} at {dpu_ip}:{port} "
            f"(timeout {STATUS_POLL_TIMEOUT_SEC}s, interval {STATUS_POLL_INTERVAL_SEC}s)"
        )
        deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SEC
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
                return True
            time.sleep(STATUS_POLL_INTERVAL_SEC)
        return False

    def _set_gnoi_shutdown_complete_flag(self, dpu_name: str, value: bool):
        """
        Set the gnoi_shutdown_complete flag in CHASSIS_MODULE_TABLE.

        This flag is used by the platform's graceful_shutdown_handler to determine
        if the gNOI shutdown has completed successfully, instead of checking oper status.

        Args:
            dpu_name: The name of the DPU module (e.g., 'DPU0')
            value: True if gNOI shutdown completed successfully, False otherwise
        """
        try:
            key = f"CHASSIS_MODULE_TABLE|{dpu_name}"
            self._db.hset(self._db.STATE_DB, key, "gnoi_shutdown_complete", "True" if value else "False")
            logger.log_info(f"Set gnoi_shutdown_complete={value} for {dpu_name}")
        except Exception as e:
            logger.log_error(f"Failed to set gnoi_shutdown_complete flag for {dpu_name}: {e}")

# #########
# Main loop
# #########

def main():
    # Connect for STATE_DB pubsub + reads and CONFIG_DB for lookups
    db = daemon_base.db_connect("STATE_DB")
    config_db = daemon_base.db_connect("CONFIG_DB")

    # Centralized transition reader
    module_base = ModuleBase()

    # gNOI reboot handler
    reboot_handler = GnoiRebootHandler(db, config_db, module_base)

    pubsub = _get_pubsub(db)

    # Listen to keyspace notifications for CHASSIS_MODULE_TABLE keys
    topic = f"__keyspace@{STATE_DB_INDEX}__:CHASSIS_MODULE_TABLE|*"
    pubsub.psubscribe(topic)

    logger.log_info("gnoi-shutdown-daemon started and listening for shutdown events.")

    while True:
        message = pubsub.get_message()
        if message and message.get("type") == "pmessage":
            channel = message.get("channel", "")
            # channel format: "__keyspace@N__:CHASSIS_MODULE_TABLE|DPU0"
            key = channel.split(":", 1)[-1] if ":" in channel else channel

            if not key.startswith("CHASSIS_MODULE_TABLE|"):
                time.sleep(1)
                continue

            # Extract module name
            try:
                dpu_name = key.split("|", 1)[1]
            except IndexError:
                time.sleep(1)
                continue

            # Read state via centralized API
            try:
                entry = module_base.get_module_state_transition(dpu_name) or {}
            except Exception as e:
                logger.log_error(f"Failed reading transition state for {dpu_name}: {e}")
                time.sleep(1)
                continue

            transition_type = entry.get("transition_type")
            if entry.get("state_transition_in_progress", "False") == "True" and (transition_type == "shutdown"):
                logger.log_info(f"{transition_type} request detected for {dpu_name}. Initiating gNOI reboot.")
                reboot_handler.handle_transition(dpu_name, transition_type)

                # NOTE:
                # For startup/shutdown transitions, the platform's graceful_shutdown_handler
                # is responsible for clearing the transition flag as a final step.
                # For reboot transitions, the reboot code is responsible for clearing the flag.

        time.sleep(1)

if __name__ == "__main__":
    main()
