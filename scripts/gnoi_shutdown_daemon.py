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
        # Use swsscommon.ConfigDBConnector for CONFIG_DB access
        from swsscommon import swsscommon
        config = swsscommon.ConfigDBConnector()
        config.connect()
        
        key = f"bridge-midplane|{dpu_name_lower}"
        entry = config.get_entry("DHCP_SERVER_IPV4_PORT", key)
        
        if entry:
            # The field is 'ips' (a list), not 'ips@'
            ips = entry.get("ips")
            if ips:
                # ips is a list, get the first IP
                ip = ips[0] if isinstance(ips, list) else ips
                logger.log_notice(f"Found DPU IP for {dpu_name}: {ip}")
                return ip
        
        logger.log_warning(f"DPU IP not found for {dpu_name}")
    except Exception as e:
        import traceback
        logger.log_error(f"Error getting DPU IP for {dpu_name}: {e}")
        logger.log_error(f"Traceback: {traceback.format_exc()}")
    
    return None

def get_dpu_gnmi_port(config_db, dpu_name: str) -> str:
    """Retrieve GNMI port from CONFIG_DB DPU table, default to 8080."""
    dpu_name_lower = dpu_name.lower()
    
    try:
        from swsscommon import swsscommon
        config = swsscommon.ConfigDBConnector()
        config.connect()
        
        # Try different key patterns for DPU table
        for k in [dpu_name_lower, dpu_name.upper(), dpu_name]:
            entry = config.get_entry("DPU", k)
            if entry and entry.get("gnmi_port"):
                port = str(entry.get("gnmi_port"))
                logger.log_notice(f"Found GNMI port for {dpu_name}: {port}")
                return port
    except Exception as e:
        logger.log_info(f"Error getting GNMI port for {dpu_name}: {e}")
    
    logger.log_info(f"GNMI port not found for {dpu_name}, using default 8080")
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
        
        This method is resilient - it logs errors but continues the gNOI sequence
        to ensure best-effort shutdown coordination.
        """
        logger.log_notice(f"=== Starting handle_transition for {dpu_name}, type={transition_type} ===")
        
        # NOTE: Do NOT set gnoi_shutdown_complete to False at the start!
        # The platform code may interpret False as "gNOI failed" and proceed with forced shutdown.
        # Only set this flag at the end of the gNOI sequence with the actual result.

        # Get DPU configuration - log error but continue with defaults if needed
        dpu_ip = None
        port = "8080"  # default
        try:
            dpu_ip = get_dpu_ip(self._config_db, dpu_name)
            port = get_dpu_gnmi_port(self._config_db, dpu_name)
            if not dpu_ip:
                logger.log_error(f"DPU IP not found for {dpu_name} - cannot proceed with gNOI")
                self._set_gnoi_shutdown_complete_flag(dpu_name, False)
                return False
            logger.log_notice(f"DPU {dpu_name} config: IP={dpu_ip}, port={port}")
        except Exception as e:
            logger.log_error(f"Error getting DPU IP or port for {dpu_name}: {e} - cannot proceed")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False

        """
        # skip if TCP is not reachable
        logger.log_notice(f"Checking TCP reachability for {dpu_name} at {dpu_ip}:{port}")
        if not is_tcp_open(dpu_ip, int(port)):
            logger.log_warning(f"Skipping {dpu_name}: {dpu_ip}:{port} unreachable (offline/down)")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)
            return False
        logger.log_notice(f"TCP port {dpu_ip}:{port} is reachable")
        """

        # NOTE: Platform code should set gnoi_halt_in_progress when ready for gNOI coordination
        # Wait for platform to complete PCI detach and set halt_in_progress flag
        logger.log_notice(f"Waiting for platform PCI detach (gnoi_halt_in_progress) for {dpu_name}")
        if not self._wait_for_gnoi_halt_in_progress(dpu_name):
            logger.log_error(f"Timeout waiting for gnoi_halt_in_progress for {dpu_name} - proceeding anyway")
        else:
            logger.log_notice(f"Platform PCI detach complete for {dpu_name}, proceeding with gNOI")

        # Send Reboot HALT (request command)
        logger.log_notice(f"Sending gNOI Reboot HALT request to {dpu_name}")
        reboot_sent = self._send_reboot_command(dpu_name, dpu_ip, port)
        if not reboot_sent:
            logger.log_error(f"Failed to send gNOI Reboot request to {dpu_name} - will still poll for status")

        # Poll RebootStatus (response command) - this completes the gNOI transaction
        logger.log_notice(f"Polling gNOI RebootStatus response for {dpu_name}")
        reboot_successful = self._poll_reboot_status(dpu_name, dpu_ip, port)

        # Set gnoi_shutdown_complete flag based on the response command result
        if reboot_successful:
            logger.log_info(f"gNOI shutdown sequence completed successfully for {dpu_name}")
            self._set_gnoi_shutdown_complete_flag(dpu_name, True)
        else:
            logger.log_error(f"gNOI shutdown sequence failed or timed out for {dpu_name}")
            self._set_gnoi_shutdown_complete_flag(dpu_name, False)

        # Clear gnoi_halt_in_progress to signal platform that daemon is done
        # Platform's _graceful_shutdown_handler waits for this flag to be cleared
        # Use the ModuleBase API via chassis.get_module() just like chassisd does
        try:
            # Get module index from DPU name (e.g., "DPU5" -> 5)
            module_index = int(dpu_name.replace("DPU", ""))
            module = self._chassis.get_module(module_index)
            module.clear_module_gnoi_halt_in_progress()
            logger.log_notice(f"Cleared gnoi_halt_in_progress flag for {dpu_name} using ModuleBase API")
        except Exception as e:
            logger.log_error(f"Failed to clear gnoi_halt_in_progress for {dpu_name}: {e}")

        logger.log_notice(f"=== Completed handle_transition for {dpu_name}, result={reboot_successful} ===")
        return reboot_successful

    def _wait_for_gnoi_halt_in_progress(self, dpu_name: str) -> bool:
        """
        Poll for gnoi_halt_in_progress flag in STATE_DB CHASSIS_MODULE_TABLE.
        
        This flag is set by the platform after completing PCI detach, signaling
        that it's safe to proceed with gNOI halt commands.
        """
        logger.log_notice(f"Polling for gnoi_halt_in_progress flag for {dpu_name} (timeout: {STATUS_POLL_TIMEOUT_SEC}s)")
        deadline = time.monotonic() + STATUS_POLL_TIMEOUT_SEC
        poll_count = 0
        
        while time.monotonic() < deadline:
            poll_count += 1
            
            try:
                # Read directly from STATE_DB using Table API (same as in main loop)
                table = swsscommon.Table(self._db, "CHASSIS_MODULE_TABLE")
                (status, fvs) = table.get(dpu_name)
                
                if status:
                    entry = dict(fvs)
                    halt_in_progress = entry.get("gnoi_halt_in_progress", "False")
                    
                    if poll_count % 3 == 1:  # Log every 3rd poll
                        logger.log_notice(f"Poll #{poll_count} for {dpu_name}: gnoi_halt_in_progress={halt_in_progress}")
                    
                    if halt_in_progress == "True":
                        logger.log_notice(f"gnoi_halt_in_progress confirmed for {dpu_name} after {poll_count} polls")
                        return True
                else:
                    logger.log_warning(f"Failed to read CHASSIS_MODULE_TABLE entry for {dpu_name}")
                    
            except Exception as e:
                logger.log_error(f"Exception reading gnoi_halt_in_progress for {dpu_name}: {e}")
            
            time.sleep(STATUS_POLL_INTERVAL_SEC)
        
        logger.log_warning(f"Timed out waiting for gnoi_halt_in_progress for {dpu_name} after {poll_count} polls ({STATUS_POLL_TIMEOUT_SEC}s)")
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

    logger.log_warning("gnoi-shutdown-daemon started and listening for CHASSIS_MODULE admin_status changes in CONFIG_DB.")

    loop_counter = 0
    while True:
        loop_counter += 1
        if loop_counter % 10 == 0:  # Log heartbeat every ~10 seconds for testing
            logger.log_warning(f"Main loop active (iteration {loop_counter})")
        
        message = pubsub.get_message(timeout=1.0)
        if message:
            msg_type = message.get("type")
            # Decode bytes to string if needed
            if isinstance(msg_type, bytes):
                msg_type = msg_type.decode('utf-8')
            
            logger.log_warning(f"Received message type: {msg_type}")
            
            if msg_type == "pmessage":
                channel = message.get("channel", b"")
                data = message.get("data", b"")
                
                # Decode bytes to string if needed
                if isinstance(channel, bytes):
                    channel = channel.decode('utf-8')
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                
                logger.log_warning(f"Keyspace event: channel={channel}, data={data}")
                
                # channel format: "__keyspace@4__:CHASSIS_MODULE|DPU0"
                key = channel.split(":", 1)[-1] if ":" in channel else channel

                if not key.startswith("CHASSIS_MODULE|"):
                    logger.log_warning(f"Ignoring non-CHASSIS_MODULE key: {key}")
                    continue

                # Extract module name
                try:
                    dpu_name = key.split("|", 1)[1]
                    if not dpu_name:
                        raise IndexError
                except IndexError:
                    logger.log_warning(f"Failed to extract DPU name from key: {key}")
                    continue

                logger.log_warning(f"CHASSIS_MODULE change detected for {dpu_name}")

                # Read admin_status from CONFIG_DB using ConfigDBConnector
                try:
                    from swsscommon import swsscommon
                    config = swsscommon.ConfigDBConnector()
                    config.connect()
                    
                    entry = config.get_entry("CHASSIS_MODULE", dpu_name)
                    if not entry:
                        logger.log_warning(f"No CHASSIS_MODULE entry found for {dpu_name}")
                        continue
                    
                    logger.log_warning(f"Module config for {dpu_name}: {entry}")
                except Exception as e:
                    import traceback
                    logger.log_error(f"Failed reading CHASSIS_MODULE config for {dpu_name}: {e}")
                    logger.log_error(f"Traceback: {traceback.format_exc()}")
                    continue

                admin_status = entry.get("admin_status", "")
                
                logger.log_warning(f"{dpu_name}: admin_status={admin_status}")
                
                if admin_status == "down":
                    # Check if we already have an active thread for this DPU
                    with active_transitions_lock:
                        if dpu_name in active_transitions:
                            logger.log_warning(f"Shutdown already in progress for {dpu_name}, skipping duplicate event")
                            continue
                        # Mark this DPU as having an active shutdown immediately to prevent race conditions
                        active_transitions.add(dpu_name)
                        logger.log_notice(f"Added {dpu_name} to active transitions set")
                    
                    logger.log_warning(f"Admin shutdown request detected for {dpu_name}. Initiating gNOI HALT.")
                    
                    # Wrapper function to clean up after transition completes
                    def handle_and_cleanup(dpu):
                        try:
                            reboot_handler.handle_transition(dpu, "shutdown")
                        finally:
                            with active_transitions_lock:
                                active_transitions.discard(dpu)
                                logger.log_info(f"Removed {dpu} from active transitions")
                    
                    # Run handle_transition in a background thread to avoid blocking the main loop
                    thread = threading.Thread(
                        target=handle_and_cleanup,
                        args=(dpu_name,),
                        name=f"gnoi-{dpu_name}",
                        daemon=True
                    )
                    thread.start()
                    logger.log_info(f"Started background thread for {dpu_name} gNOI shutdown handling")
                else:
                    logger.log_warning(f"Admin status not 'down' for {dpu_name}: admin_status={admin_status}")

if __name__ == "__main__":
    main()