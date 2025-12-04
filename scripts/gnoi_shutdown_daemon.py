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
import os
import redis
import threading
import sonic_py_common.daemon_base as daemon_base
from sonic_py_common import syslogger
from swsscommon import swsscommon

REBOOT_RPC_TIMEOUT_SEC = 60  # gNOI System.Reboot call timeout
STATUS_POLL_TIMEOUT_SEC = 60  # overall time - polling RebootStatus
STATUS_POLL_INTERVAL_SEC = 1  # delay between reboot status polls
HALT_IN_PROGRESS_POLL_INTERVAL_SEC = 5  # delay between halt_in_progress checks
STATUS_RPC_TIMEOUT_SEC = 10  # per RebootStatus RPC timeout
REBOOT_METHOD_HALT = 3  # gNOI System.Reboot method: HALT
STATE_DB_INDEX = 6
CONFIG_DB_INDEX = 4
DEFAULT_GNMI_PORT = "8080"  # Default GNMI port for DPU

SYSLOG_IDENTIFIER = "gnoi-shutdown-daemon"
logger = syslogger.SysLogger(SYSLOG_IDENTIFIER)


# ##########
# Helpers
# ##########


def _get_halt_timeout() -> int:
    """Get halt_services timeout from platform.json, or default to STATUS_POLL_TIMEOUT_SEC."""
    try:
        from sonic_platform import platform
        chassis = platform.Platform().get_chassis()
        platform_name = chassis.get_name() if hasattr(chassis, 'get_name') else None

        if not platform_name:
            return STATUS_POLL_TIMEOUT_SEC

        platform_json_path = f"/usr/share/sonic/device/{platform_name}/platform.json"

        if os.path.exists(platform_json_path):
            with open(platform_json_path, 'r') as f:
                return int(json.load(f).get("dpu_halt_services_timeout", STATUS_POLL_TIMEOUT_SEC))
    except (OSError, IOError, ValueError, KeyError) as e:
        logger.log_info(f"Could not load timeout from platform.json: {e}, using default {STATUS_POLL_TIMEOUT_SEC}s")
    return STATUS_POLL_TIMEOUT_SEC


def execute_command(command_args, timeout_sec=REBOOT_RPC_TIMEOUT_SEC, suppress_stderr=False):
    """Run gnoi_client with a timeout; return (rc, stdout, stderr)."""
    try:
        stderr_dest = subprocess.DEVNULL if suppress_stderr else subprocess.PIPE
        result = subprocess.run(command_args, stdout=subprocess.PIPE, stderr=stderr_dest, text=True, timeout=timeout_sec)
        return result.returncode, result.stdout.strip(), result.stderr.strip() if not suppress_stderr else ""
    except subprocess.TimeoutExpired as e:
        return -1, "", f"Command timed out after {int(e.timeout)}s."
    except Exception as e:
        return -2, "", f"Command failed: {e}"


def get_dpu_ip(config_db, dpu_name: str) -> str:
    """Retrieve DPU IP from CONFIG_DB DHCP_SERVER_IPV4_PORT table."""
    dpu_name_lower = dpu_name.lower()

    try:
        key = f"DHCP_SERVER_IPV4_PORT|bridge-midplane|{dpu_name_lower}"
        ips = config_db.hget(key, "ips@")

        if ips:
            if isinstance(ips, bytes):
                ips = ips.decode('utf-8')
            ip = ips[0] if isinstance(ips, list) else ips
            return ip

    except (AttributeError, KeyError, TypeError) as e:
        logger.log_error(f"{dpu_name}: Error getting IP: {e}")

    return None


def get_dpu_gnmi_port(config_db, dpu_name: str) -> str:
    """Retrieve GNMI port from CONFIG_DB DPU table, default to 8080."""
    dpu_name_lower = dpu_name.lower()

    try:
        for k in [dpu_name_lower, dpu_name.upper(), dpu_name]:
            key = f"DPU|{k}"
            gnmi_port = config_db.hget(key, "gnmi_port")
            if gnmi_port:
                if isinstance(gnmi_port, bytes):
                    gnmi_port = gnmi_port.decode('utf-8')
                return str(gnmi_port)
    except (AttributeError, KeyError, TypeError) as e:
        logger.log_warning(f"{dpu_name}: Error getting gNMI port, using default: {e}")

    logger.log_info(f"{dpu_name}: gNMI port not found, using default {DEFAULT_GNMI_PORT}")
    return DEFAULT_GNMI_PORT

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

    def _handle_transition(self, dpu_name: str, transition_type: str) -> bool:
        """
        Handle a shutdown or reboot transition for a DPU module.
        Returns True if the operation completed successfully, False otherwise.
        """
        logger.log_notice(f"{dpu_name}: Starting gNOI shutdown sequence")

        # Wait for platform PCI detach completion
        if not self._wait_for_gnoi_halt_in_progress(dpu_name):
            logger.log_warning(f"{dpu_name}: Timeout waiting for PCI detach, proceeding anyway")

        # Get DPU configuration
        dpu_ip = None
        try:
            dpu_ip = get_dpu_ip(self._config_db, dpu_name)
            port = get_dpu_gnmi_port(self._config_db, dpu_name)
            if not dpu_ip:
                logger.log_error(f"{dpu_name}: IP not found in DHCP_SERVER_IPV4_PORT table (key: bridge-midplane|{dpu_name.lower()}), cannot proceed")
                self._clear_halt_flag(dpu_name)
                return False
        except Exception as e:
            logger.log_error(f"{dpu_name}: Failed to get configuration: {e}")
            self._clear_halt_flag(dpu_name)
            return False

        # Send gNOI Reboot HALT command
        reboot_sent = self._send_reboot_command(dpu_name, dpu_ip, port)
        if not reboot_sent:
            logger.log_error(f"{dpu_name}: Failed to send Reboot command")
            self._clear_halt_flag(dpu_name)
            return False

        # Poll for RebootStatus completion
        reboot_successful = self._poll_reboot_status(dpu_name, dpu_ip, port)

        if self._clear_halt_flag(dpu_name):
            logger.log_notice(f"{dpu_name}: Halting the services on DPU is successful for {dpu_name}")

        return reboot_successful

    def _wait_for_gnoi_halt_in_progress(self, dpu_name: str) -> bool:
        """
        Poll for gnoi_halt_in_progress flag in STATE_DB CHASSIS_MODULE_TABLE.
        This flag is set by the platform after completing PCI detach.
        """
        deadline = time.monotonic() + _get_halt_timeout()

        while time.monotonic() < deadline:
            try:
                table = swsscommon.Table(self._db, "CHASSIS_MODULE_TABLE")
                (status, fvs) = table.get(dpu_name)

                if status:
                    entry = dict(fvs)
                    halt_in_progress = entry.get("gnoi_halt_in_progress", "False")

                    if halt_in_progress == "True":
                        logger.log_notice(f"{dpu_name}: PCI detach complete, proceeding for halting services via gNOI")
                        return True

            except Exception as e:
                logger.log_error(f"{dpu_name}: Error reading halt flag: {e}")

            time.sleep(HALT_IN_PROGRESS_POLL_INTERVAL_SEC)

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
        rc, out, err = execute_command(reboot_cmd, timeout_sec=REBOOT_RPC_TIMEOUT_SEC, suppress_stderr=True)
        if rc != 0:
            logger.log_error(f"{dpu_name}: Reboot command failed")
            return False
        return True

    def _poll_reboot_status(self, dpu_name: str, dpu_ip: str, port: str) -> bool:
        """Poll RebootStatus until completion or timeout."""
        deadline = time.monotonic() + _get_halt_timeout()
        status_cmd = [
            "docker", "exec", "gnmi", "gnoi_client",
            f"-target={dpu_ip}:{port}",
            "-logtostderr", "-notls",
            "-module", "System",
            "-rpc", "RebootStatus"
        ]
        while time.monotonic() < deadline:
            rc_s, out_s, err_s = execute_command(status_cmd, timeout_sec=STATUS_RPC_TIMEOUT_SEC)
            if rc_s == 0 and out_s and ("reboot complete" in out_s.lower()):
                return True
            time.sleep(STATUS_POLL_INTERVAL_SEC)
        logger.log_notice(f"{dpu_name}: Timeout waiting for RebootStatus completion, proceeding with halt flag clear")
        return False

    def _clear_halt_flag(self, dpu_name: str) -> bool:
        """Clear halt_in_progress flag via platform API."""
        try:
            # Use chassis.get_module_index() to get the correct platform index for the named module
            module_index = self._chassis.get_module_index(dpu_name)
            if module_index < 0:
                logger.log_error(f"{dpu_name}: Unable to get module index from chassis")
                return False
            
            module = self._chassis.get_module(module_index)
            if module is None:
                logger.log_error(f"{dpu_name}: Module at index {module_index} not found in chassis")
                return False
            
            module.clear_module_gnoi_halt_in_progress()
            logger.log_info(f"{dpu_name}: Successfully cleared halt_in_progress flag (module index: {module_index})")
            return True
        except Exception as e:
            logger.log_error(f"{dpu_name}: Failed to clear halt flag: {e}")
            return False

# #########
# Main loop
# #########

def main():
    # Connect for STATE_DB (for gnoi_halt_in_progress flag) and CONFIG_DB
    state_db = daemon_base.db_connect("STATE_DB")
    config_db = daemon_base.db_connect("CONFIG_DB")

    # Also connect ConfigDBConnector for pubsub support (has get_redis_client method)
    config_db_connector = swsscommon.ConfigDBConnector()
    config_db_connector.connect(wait_for_init=False)

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

    # Keyspace notifications are globally enabled in docker-database
    pubsub = config_db_connector.get_redis_client(config_db_connector.db_name).pubsub()

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
                if isinstance(channel, bytes):
                    channel = channel.decode('utf-8')

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
                    key = f"CHASSIS_MODULE|{dpu_name}"
                    admin_status = config_db.hget(key, "admin_status")
                    if not admin_status:
                        continue

                    if isinstance(admin_status, bytes):
                        admin_status = admin_status.decode('utf-8')

                except (AttributeError, KeyError, TypeError) as e:
                    logger.log_error(f"{dpu_name}: Failed to read CONFIG_DB: {e}")
                    continue

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
                            reboot_handler._handle_transition(dpu, "shutdown")
                            logger.log_info(f"{dpu}: Transition thread completed successfully")
                        except Exception as e:
                            logger.log_error(f"{dpu}: Transition thread failed with exception: {e}")
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
