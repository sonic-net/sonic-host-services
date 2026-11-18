#!/usr/bin/env python3
"""
gnoi-shutdown-daemon

Listens for CHASSIS_MODULE_TABLE state changes in STATE_DB and, when a
SmartSwitch DPU module enters a "shutdown" transition, issues a gNOI Reboot
(method HALT) toward that DPU and polls RebootStatus until complete or timeout.
"""

import json
import time
import os
import ssl
import threading
from typing import NamedTuple, Optional

import grpc
from sonic_grpc.gnoi import GnoiClient, system_pb2

import sonic_py_common.daemon_base as daemon_base
from sonic_platform_base.module_base import ModuleBase
from sonic_py_common import syslogger, device_info
from swsscommon import swsscommon
from utilities_common.chassis import is_dpu

REBOOT_RPC_TIMEOUT_SEC = 60  # gNOI System.Reboot call timeout
STATUS_POLL_TIMEOUT_SEC = 60  # overall time - polling RebootStatus
STATUS_POLL_INTERVAL_SEC = 1  # delay between reboot status polls
HALT_IN_PROGRESS_POLL_INTERVAL_SEC = 5  # delay between halt_in_progress checks
STATUS_RPC_TIMEOUT_SEC = 10  # per RebootStatus RPC timeout
PORT_PROBE_TIMEOUT_SEC = 10  # total budget per candidate port: cert fetch + System.Time combined
CONFIG_DB_INDEX = 4
COMMON_GNMI_PORTS = ("8080", "50052")

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


def get_dpu_gnmi_ports(config_db, dpu_name: str):
    """Return configured and common DPU gNMI ports in preference order."""
    dpu_name_lower = dpu_name.lower()
    configured_port = None

    try:
        for k in [dpu_name_lower, dpu_name.upper(), dpu_name]:
            key = f"DPU|{k}"
            gnmi_port = config_db.hget(key, "gnmi_port")
            if gnmi_port:
                if isinstance(gnmi_port, bytes):
                    gnmi_port = gnmi_port.decode('utf-8')
                configured_port = str(gnmi_port)
                break
    except (AttributeError, KeyError, TypeError) as e:
        logger.log_warning(f"{dpu_name}: Error getting configured gNMI port: {e}")

    ports = []
    for port in (configured_port, *COMMON_GNMI_PORTS):
        if port and port not in ports:
            ports.append(port)
    return ports


# #########################
# DPU TLS (isolated on purpose)
# #########################
#
# DPU gNOI/gNMI servers generate a new self-signed certificate at every
# startup, so there is no stable CA to trust ahead of time. Per SONiC
# maintainer guidance (sonic-net/sonic-buildimage#29188), the fetched
# certificate is pinned directly as root_certificates for that same
# connection -- this is ephemeral-certificate pinning, not plaintext: the
# RPC channel itself is always encrypted TLS.
#
# Kept in small, isolated functions so this can be swapped out cleanly if
# SONiC settles on a different DPU trust policy.


class DpuEndpoint(NamedTuple):
    """A DPU gNMI/gNOI port that has been probed successfully, plus the
    channel credentials pinned to its current certificate — reused for
    Reboot and RebootStatus so we don't redo the TLS fetch per RPC."""
    port: str
    credentials: "grpc.ChannelCredentials"


def _fetch_dpu_cert_pem(dpu_ip: str, port: str, timeout: float) -> bytes:
    """Unverified TLS fetch of the DPU's current ephemeral certificate —
    there is nothing to verify it against yet. TLS is required for this
    fetch; any failure here (unreachable, handshake error, or a malformed
    port -- e.g. a bad configured gnmi_port -- surfacing as a DNS/socket
    error) must be treated by the caller as this candidate port failing,
    never a plaintext fallback.
    """
    pem = ssl.get_server_certificate((dpu_ip, port), timeout=timeout)
    return pem.encode()


def _build_dpu_endpoint(dpu_ip: str, port: str, timeout: float) -> DpuEndpoint:
    """Fetch the DPU's current ephemeral certificate and pin the gRPC
    channel credentials to it. Raises on any TLS failure."""
    pem = _fetch_dpu_cert_pem(dpu_ip, port, timeout)
    credentials = grpc.ssl_channel_credentials(root_certificates=pem)
    return DpuEndpoint(port=port, credentials=credentials)


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

    def _should_skip_gnoi_shutdown(self, dpu_name: str):
        """
        Check whether the DPU is already offline / powered-down.

        Returns:
            True  - DPU is known to be offline/powered-down; skip gNOI shutdown.
            False - DPU is known to be in another state; proceed with gNOI shutdown.
            None  - Cannot determine status; caller should proceed with gNOI shutdown.
        """
        module_index = self._chassis.get_module_index(dpu_name)
        if module_index < 0:
            return None

        module = self._chassis.get_module(module_index)
        if module is None:
            return None

        oper_status = module.get_oper_status()
        return oper_status in (
            ModuleBase.MODULE_STATUS_OFFLINE,
            ModuleBase.MODULE_STATUS_POWERED_DOWN,
        )

    def _handle_transition(self, dpu_name: str,
                           config_db=None, state_db=None) -> bool:
        """
        Handle a shutdown or reboot transition for a DPU module.
        Returns True if the operation completed successfully, False otherwise.

        config_db/state_db are per-thread DB connections; fall back to the
        shared connections for direct/unit-test callers.
        """
        config_db = config_db if config_db is not None else self._config_db
        state_db = state_db if state_db is not None else self._db

        logger.log_notice(f"{dpu_name}: Starting gNOI shutdown sequence")

        # Check if DPU is already powered off / offline before attempting gNOI shutdown.
        # This avoids error logs when config reload or reboot is issued while DPUs are
        # already in the down state (e.g. admin_status was previously set to "down").
        try:
            skip = self._should_skip_gnoi_shutdown(dpu_name)
        except Exception as e:
            logger.log_warning(
                f"{dpu_name}: Could not determine operational status ({e}), "
                "proceeding with gNOI shutdown"
            )
            skip = False

        if skip:
            logger.log_notice(
                f"{dpu_name}: DPU is already offline/powered-down, "
                "skipping gNOI shutdown sequence"
            )
            cleared = self._clear_halt_flag(dpu_name)
            if not cleared:
                logger.log_warning(
                    f"{dpu_name}: Failed to clear halt flag while skipping gNOI shutdown"
                )
            return cleared

        # Wait for platform PCI detach completion
        if not self._wait_for_gnoi_halt_in_progress(dpu_name, state_db):
            logger.log_warning(f"{dpu_name}: Timeout waiting for PCI detach, proceeding anyway")

        # Get DPU configuration
        dpu_ip = None
        try:
            dpu_ip = get_dpu_ip(config_db, dpu_name)
            ports = get_dpu_gnmi_ports(config_db, dpu_name)
            if not dpu_ip:
                logger.log_error(f"{dpu_name}: IP not found in DHCP_SERVER_IPV4_PORT table (key: bridge-midplane|{dpu_name.lower()}), cannot proceed")
                self._clear_halt_flag(dpu_name)
                return False
        except Exception as e:
            logger.log_error(f"{dpu_name}: Failed to get configuration: {e}")
            self._clear_halt_flag(dpu_name)
            return False

        endpoint = self._find_working_port(dpu_name, dpu_ip, ports)
        if endpoint is None:
            logger.log_error(f"{dpu_name}: No reachable gNMI port found")
            self._clear_halt_flag(dpu_name)
            return False

        # Send gNOI Reboot HALT command
        reboot_sent = self._send_reboot_command(dpu_name, dpu_ip, endpoint)
        if not reboot_sent:
            logger.log_error(f"{dpu_name}: Failed to send Reboot command")
            self._clear_halt_flag(dpu_name)
            return False

        # Poll for RebootStatus completion
        reboot_successful = self._poll_reboot_status(dpu_name, dpu_ip, endpoint)

        if self._clear_halt_flag(dpu_name):
            logger.log_notice(f"{dpu_name}: Halting the services on DPU is successful for {dpu_name}")

        return reboot_successful

    def _wait_for_gnoi_halt_in_progress(self, dpu_name: str, state_db=None) -> bool:
        """
        Poll for gnoi_halt_in_progress flag in STATE_DB CHASSIS_MODULE_TABLE.
        This flag is set by the platform after completing PCI detach.
        """
        state_db = state_db if state_db is not None else self._db
        deadline = time.monotonic() + _get_halt_timeout()

        while time.monotonic() < deadline:
            try:
                table = swsscommon.Table(state_db, "CHASSIS_MODULE_TABLE")
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

    def _probe_port(self, dpu_name: str, dpu_ip: str, port: str) -> Optional[DpuEndpoint]:
        """Probe a single port: fetch/pin its current certificate, then
        confirm it's actually a live gNOI endpoint via System.Time. Returns
        the endpoint only on an actually successful Time RPC.

        Certificate fetch and the Time RPC share ONE overall
        PORT_PROBE_TIMEOUT_SEC budget for this candidate port, not two
        independent timeouts -- otherwise an unreachable port could cost
        up to 2x PORT_PROBE_TIMEOUT_SEC, and probing all of
        configured+8080+50052 could add proportionally more delay before
        Reboot is even attempted.
        """
        probe_deadline = time.monotonic() + PORT_PROBE_TIMEOUT_SEC
        try:
            endpoint = _build_dpu_endpoint(dpu_ip, port, PORT_PROBE_TIMEOUT_SEC)
        except (ssl.SSLError, OSError) as e:
            logger.log_warning(f"{dpu_name}: TLS setup failed (target={dpu_ip}:{port}): {e}")
            return None

        remaining = probe_deadline - time.monotonic()
        if remaining <= 0:
            logger.log_warning(f"{dpu_name}: probe budget exhausted after certificate fetch (target={dpu_ip}:{port})")
            return None

        try:
            with GnoiClient(f"{dpu_ip}:{port}", credentials=endpoint.credentials) as client:
                client.system.Time(system_pb2.TimeRequest(), timeout=remaining)
            return endpoint
        except grpc.RpcError as e:
            logger.log_warning(f"{dpu_name}: gNMI probe failed (target={dpu_ip}:{port}): {e.code()}: {e.details()}")
            return None

    def _find_working_port(self, dpu_name: str, dpu_ip: str, ports) -> Optional[DpuEndpoint]:
        """Return the endpoint for the first port that responds to System.Time.

        Each probe (TLS fetch + RPC) can block for up to PORT_PROBE_TIMEOUT_SEC,
        so probing multiple unreachable ports adds proportional delay to
        graceful shutdown.
        """
        for port in ports:
            endpoint = self._probe_port(dpu_name, dpu_ip, port)
            if endpoint is not None:
                return endpoint
        return None

    def _send_reboot_command(self, dpu_name: str, dpu_ip: str, endpoint: DpuEndpoint) -> bool:
        """Send one gNOI Reboot HALT command to the selected DPU endpoint."""
        try:
            with GnoiClient(f"{dpu_ip}:{endpoint.port}", credentials=endpoint.credentials) as client:
                client.system.Reboot(
                    system_pb2.RebootRequest(
                        method=system_pb2.HALT,
                        message="Triggered by SmartSwitch graceful shutdown",
                    ),
                    timeout=REBOOT_RPC_TIMEOUT_SEC,
                )
            return True
        except grpc.RpcError as e:
            logger.log_error(f"{dpu_name}: Reboot command failed (target={dpu_ip}:{endpoint.port}): {e.code()}: {e.details()}")
            return False

    def _poll_reboot_status(self, dpu_name: str, dpu_ip: str, endpoint: DpuEndpoint) -> bool:
        """Poll RebootStatus on the selected endpoint until completion or timeout.

        Completion is a successful, non-active RebootStatusResponse whose
        status is either absent (legacy servers) or explicitly SUCCESS.
        Any other observed state (active=True, an explicit non-SUCCESS
        status, or an RPC error such as the DPU going unreachable right
        after HALT) is treated as "not yet confirmed" and polling
        continues until the halt timeout, matching the accepted behavior
        in sonic-utilities' reboot_smartswitch_helper.
        """
        deadline = time.monotonic() + _get_halt_timeout()
        logged_error = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            status_timeout = min(STATUS_RPC_TIMEOUT_SEC, remaining)

            try:
                with GnoiClient(f"{dpu_ip}:{endpoint.port}", credentials=endpoint.credentials) as client:
                    resp = client.system.RebootStatus(system_pb2.RebootStatusRequest(), timeout=status_timeout)
                if not resp.active and (
                    not resp.HasField("status")
                    or resp.status.status == system_pb2.RebootStatus.Status.STATUS_SUCCESS
                ):
                    return True
                logged_error = False
            except grpc.RpcError as e:
                # The DPU is expected to become unreachable once it actually
                # halts, so don't spam a warning on every 1s retry.
                if not logged_error:
                    logger.log_warning(f"{dpu_name}: RebootStatus probe failed (target={dpu_ip}:{endpoint.port}): {e.code()}: {e.details()}")
                    logged_error = True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(STATUS_POLL_INTERVAL_SEC, remaining))

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
                            # Per-thread DB connections: a redis connection is a
                            # single non-thread-safe socket, so sharing one lets
                            # concurrent DPU reads cross their IP/port values.
                            thread_config_db = daemon_base.db_connect("CONFIG_DB")
                            thread_state_db = daemon_base.db_connect("STATE_DB")
                            reboot_handler._handle_transition(
                                dpu,
                                config_db=thread_config_db,
                                state_db=thread_state_db)
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
