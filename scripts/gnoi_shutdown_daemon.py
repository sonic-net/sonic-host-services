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
import redis
import threading
import sonic_py_common.daemon_base as daemon_base
from swsscommon import swsscommon

REBOOT_RPC_TIMEOUT_SEC   = 60   # gNOI System.Reboot call timeout
STATUS_POLL_TIMEOUT_SEC  = 60   # overall time - polling RebootStatus
STATUS_POLL_INTERVAL_SEC = 5    # delay between polls
STATUS_RPC_TIMEOUT_SEC   = 10   # per RebootStatus RPC timeout
REBOOT_METHOD_HALT = 3          # gNOI System.Reboot method: HALT
STATE_DB_INDEX = 6
CONFIG_DB_INDEX = 4

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

def _get_pubsub(db_index):
    """Return a pubsub object for keyspace notifications.
    
    Args:
        db_index: The Redis database index (e.g., 4 for CONFIG_DB, 6 for STATE_DB)
    """
    # Connect directly to Redis using redis-py
    redis_client = redis.Redis(unix_socket_path='/var/run/redis/redis.sock', db=db_index)
    return redis_client.pubsub()

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
    """Retrieve DPU IP from CONFIG_DB DHCP_SERVER_IPV4_PORT table."""
    dpu_name_lower = dpu_name.lower()
    
    try:
        from swsscommon import swsscommon
        config = swsscommon.ConfigDBConnector()
        config.connect()
        
        key = f"bridge-midplane|{dpu_name_lower}"
        entry = config.get_entry("DHCP_SERVER_IPV4_PORT", key)
        
        if entry:
            ips = entry.get("ips")
            if ips:
                ip = ips[0] if isinstance(ips, list) else ips
                return ip
        
    except Exception as e:
        logger.log_error(f"{dpu_name}: Error getting IP: {e}")
    
    return None

def get_dpu_gnmi_port(config_db, dpu_name: str) -> str:
    """Retrieve GNMI port from CONFIG_DB DPU table, default to 8080."""
    dpu_name_lower = dpu_name.lower()
    
    try:
        from swsscommon import swsscommon
        config = swsscommon.ConfigDBConnector()
        config.connect()
        
        for k in [dpu_name_lower, dpu_name.upper(), dpu_name]:
            entry = config.get_entry("DPU", k)
            if entry and entry.get("gnmi_port"):
                return str(entry.get("gnmi_port"))
    except Exception as e:
        logger.log_warning(f"{dpu_name}: Error getting gNMI port, using default: {e}")
    
    return "8080"

# ###############
# gNOI Reboot Handler
# ###############
class GnoiRebootHandler:
    """
    Handles gNOI reboot operations for DPU modules, including sending reboot commands
    and polling for status completion.
    """
    def __init__(self, db, config_db, chassis):
        self._db = db
        self._config_db = config_db
        self._chassis = chassis

    def handle_transition(self, dpu_name: str, transition_type: str) -> bool:
        """
        Handle a shutdown or reboot transition for a DPU module.
        Returns True if the operation completed successfully, False otherwise.
        """
        logger.log_notice(f"{dpu_name}: Starting gNOI shutdown sequence")
        
        # Get DPU configuration
        dpu_ip = None
        port = "8080"
        try:
            dpu_ip = get_dpu_ip(self._config_db, dpu_name)
            port = get_dpu_gnmi_port(self._config_db, dpu_name)
            if not dpu_ip:
                logger.log_error(f"{dpu_name}: IP not found in DHCP_SERVER_IPV4_PORT table (key: bridge-midplane|{dpu_name.lower()}), cannot proceed")
                self._set_gnoi_shutdown_complete_flag(dpu_name, False)
                return False
        except Exception as e:
            logger.log_error(f"{dpu_name}: Failed to get configuration: {e}")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        # Wait for platform PCI detach completion
        if not self._wait_for_gnoi_halt_in_progress(dpu_name):
            logger.log_warning(f"{dpu_name}: Timeout waiting for PCI detach, proceeding anyway")

        # Send gNOI Reboot HALT command
        reboot_sent = self._send_reboot_command(dpu_name, dpu_ip, port)
        if not reboot_sent:
            logger.log_error(f"{dpu_name}: Failed to send Reboot command")

        # Poll for RebootStatus completion
        reboot_successful = self._poll_reboot_status(dpu_name, dpu_ip, port)

        # Set completion flag
        self._set_gnoi_shutdown_complete_flag(dpu_name, reboot_successful)
        
        # Clear halt_in_progress to signal platform
        try:
            module_index = int(dpu_name.replace("DPU", ""))
            self._chassis.get_module(module_index).clear_module_gnoi_halt_in_progress()
            logger.log_notice(f"{dpu_name}: gNOI sequence {'completed' if reboot_successful else 'failed'}")
        except Exception as e:
            logger.log_error(f"{dpu_name}: Failed to clear halt flag: {e}")

        return reboot_successful

    def _wait_for_gnoi_halt_in_progress(self, dpu_name: str) -> bool:
        """
        Poll for gnoi_halt_in_progress flag in STATE_DB CHASSIS_MODULE_TABLE.
        This flag is set by the platform after completing PCI detach.
        """
        deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SEC
        poll_count = 0
        
        while time.monotonic() < deadline:
            poll_count += 1
            
            try:
                table = swsscommon.Table(self._db, "CHASSIS_MODULE_TABLE")
                (status, fvs) = table.get(dpu_name)
                
                if status:
                    entry = dict(fvs)
                    halt_in_progress = entry.get("gnoi_halt_in_progress", "False")
                    
                    if halt_in_progress == "True":
                        logger.log_notice(f"{dpu_name}: PCI detach complete, proceeding with gNOI")
                        return True
                    
            except Exception as e:
                logger.log_error(f"{dpu_name}: Error reading halt flag: {e}")
            
            time.sleep(STATUS_POLL_INTERVAL_SEC)
        
        return False

    def _send_reboot_command(self, dpu_name: str, dpu_ip: str, port: str) -> bool:
        """Send gNOI Reboot HALT command to the DPU."""
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
            logger.log_error(f"{dpu_name}: Reboot command failed - {err or out}")
            return False
        return True

    def _poll_reboot_status(self, dpu_name: str, dpu_ip: str, port: str) -> bool:
        """Poll RebootStatus until completion or timeout."""
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
            table = swsscommon.Table(self._db, "CHASSIS_MODULE_TABLE")
            fvs = swsscommon.FieldValuePairs([("gnoi_shutdown_complete", "True" if value else "False")])
            table.set(dpu_name, fvs)
            logger.log_info(f"Set gnoi_shutdown_complete={value} for {dpu_name}")
        except Exception as e:
            logger.log_error(f"Failed to set gnoi_shutdown_complete flag for {dpu_name}: {e}")

# #########
# Main loop
# #########

def main():
    # Connect for STATE_DB (for gnoi_halt_in_progress flag) and CONFIG_DB
    state_db = daemon_base.db_connect("STATE_DB")
    config_db = daemon_base.db_connect("CONFIG_DB")

    # Get chassis instance for accessing ModuleBase APIs
    try:
        from sonic_platform import platform
        chassis = platform.Platform().get_chassis()
        logger.log_info("Successfully obtained chassis instance")
    except Exception as e:
        logger.log_error(f"Failed to get chassis instance: {e}")
        raise

    # gNOI reboot handler
    reboot_handler = GnoiRebootHandler(state_db, config_db, chassis)
    
    # Track active transitions to prevent duplicate threads for the same DPU
    active_transitions = set()
    active_transitions_lock = threading.Lock()

    # Enable keyspace notifications for CONFIG_DB
    try:
        # Connect directly to Redis using redis-py to enable keyspace notifications
        redis_client = redis.Redis(unix_socket_path='/var/run/redis/redis.sock', db=CONFIG_DB_INDEX)
        redis_client.config_set('notify-keyspace-events', 'KEA')
        logger.log_info("Keyspace notifications enabled successfully for CONFIG_DB")
    except Exception as e:
        logger.log_warning(f"Failed to enable keyspace notifications: {e}")

    pubsub = _get_pubsub(CONFIG_DB_INDEX)

    # Listen to keyspace notifications for CHASSIS_MODULE table keys in CONFIG_DB
    topic = f"__keyspace@{CONFIG_DB_INDEX}__:CHASSIS_MODULE|*"
    pubsub.psubscribe(topic)

    logger.log_notice("gnoi-shutdown-daemon started, monitoring CHASSIS_MODULE admin_status changes")

    while True:
        message = pubsub.get_message(timeout=1.0)
        if message:
            msg_type = message.get("type")
            if isinstance(msg_type, bytes):
                msg_type = msg_type.decode('utf-8')
            
            if msg_type == "pmessage":
                channel = message.get("channel", b"")
                data = message.get("data", b"")
                
                if isinstance(channel, bytes):
                    channel = channel.decode('utf-8')
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                
                # Extract key from channel: "__keyspace@4__:CHASSIS_MODULE|DPU0"
                key = channel.split(":", 1)[-1] if ":" in channel else channel

                if not key.startswith("CHASSIS_MODULE|"):
                    continue

                # Extract module name
                try:
                    dpu_name = key.split("|", 1)[1]
                    if not dpu_name:
                        raise IndexError
                except IndexError:
                    continue

                # Read admin_status from CONFIG_DB
                try:
                    config = swsscommon.ConfigDBConnector()
                    config.connect()
                    
                    entry = config.get_entry("CHASSIS_MODULE", dpu_name)
                    if not entry:
                        continue
                    
                except Exception as e:
                    logger.log_error(f"{dpu_name}: Failed to read CONFIG_DB: {e}")
                    continue

                admin_status = entry.get("admin_status", "")
                
                if admin_status == "down":
                    # Check if already processing this DPU
                    with active_transitions_lock:
                        if dpu_name in active_transitions:
                            continue
                        active_transitions.add(dpu_name)
                    
                    logger.log_notice(f"{dpu_name}: Admin shutdown detected, initiating gNOI HALT")
                    
                    # Wrapper to clean up after transition
                    def handle_and_cleanup(dpu):
                        try:
                            reboot_handler.handle_transition(dpu, "shutdown")
                        finally:
                            with active_transitions_lock:
                                active_transitions.discard(dpu)
                    
                    # Run in background thread
                    thread = threading.Thread(
                        target=handle_and_cleanup,
                        args=(dpu_name,),
                        name=f"gnoi-{dpu_name}",
                        daemon=True
                    )
                    thread.start()

if __name__ == "__main__":
    main()