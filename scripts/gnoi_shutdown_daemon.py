#!/usr/bin/env python3

"""
gnoi-shutdown-daemon

This daemon facilitates gNOI-based shutdown operations for DPU subcomponents within the SONiC platform.
It listens to Redis STATE_DB changes on CHASSIS_MODULE_INFO_TABLE and triggers gNOI-based HALT
for DPU modules only when a shutdown transition is detected.

The daemon is intended to run on SmartSwitch NPU only (not on DPU modules).
"""

try:
    import json
    import time
    import subprocess
    from swsssdk import SonicV2Connector
    from sonic_py_common import syslogger
except ImportError as err:
    raise ImportError("%s - required module not found" % str(err))

SYSLOG_IDENTIFIER = "gnoi-shutdown-daemon"
logger = syslogger.SysLogger(SYSLOG_IDENTIFIER)

def execute_gnoi_command(command_args):
    try:
        result = subprocess.run(command_args, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out."

def get_dpu_ip(dpu_name):
    db = SonicV2Connector()
    db.connect(db.CONFIG_DB)
    key = f"bridge-midplane|{dpu_name}"
    entry = db.get_entry("DHCP_SERVER_IPV4_PORT", key)
    return entry.get("ips@")

def get_gnmi_port(dpu_name):
    db = SonicV2Connector()
    db.connect(db.CONFIG_DB)
    entry = db.get_entry("DPU_PORT", dpu_name)
    return entry.get("gnmi_port", "8080")

def get_reboot_timeout():
    db = SonicV2Connector()
    db.connect(db.CONFIG_DB)
    platform = db.get_entry("DEVICE_METADATA", "localhost").get("platform")
    if not platform:
        return 60
    platform_json_path = f"/usr/share/sonic/device/{platform}/platform.json"
    try:
        with open(platform_json_path, "r") as f:
            data = json.load(f)
            timeout = data.get("dpu_halt_services_timeout")
            if not timeout:
                return 60
            return int(timeout)
    except Exception:
        return 60

def main():
    db = SonicV2Connector()
    db.connect(db.STATE_DB)
    pubsub = db.pubsub()
    pubsub.psubscribe("__keyspace@6__:CHASSIS_MODULE_INFO_TABLE|*")

    logger.log_info("gnoi-shutdown-daemon started and listening for shutdown events.")

    while True:
        message = pubsub.get_message()
        if message and message['type'] == 'pmessage':
            key = message['channel'].split(":")[-1]  # e.g., CHASSIS_MODULE_INFO_TABLE|DPU0
            if not key.startswith("CHASSIS_MODULE_INFO_TABLE|"):
                continue

            dpu_name = key.split("|")[1]
            entry = db.get_all(db.STATE_DB, key)
            if not entry:
                continue

            transition = entry.get("state_transition_in_progress")
            transition_type = entry.get("transition_type")

            if transition == "True" and transition_type == "shutdown":
                logger.log_info(f"Shutdown request detected for {dpu_name}. Initiating gNOI reboot.")
                try:
                    dpu_ip = get_dpu_ip(dpu_name)
                    port = get_gnmi_port(dpu_name)
                except Exception as e:
                    logger.log_error(f"Error getting DPU IP or port: {e}")
                    continue

                reboot_cmd = [
                    "docker", "exec", "gnmi", "gnoi_client",
                    f"-target={dpu_ip}:{port}",
                    "-logtostderr", "-notls",
                    "-module", "System",
                    "-rpc", "Reboot",
                    "-jsonin", json.dumps({"method": 3, "message": "Triggered by SmartSwitch graceful shutdown"})
                ]

                returncode, stdout, stderr = execute_gnoi_command(reboot_cmd)
                if returncode != 0:
                    logger.log_error(f"gNOI Reboot command failed for {dpu_name}: {stderr}")
                    continue

                timeout = get_reboot_timeout()
                interval = 5
                elapsed = 0
                reboot_successful = False

                while elapsed < timeout:
                    status_cmd = [
                        "docker", "exec", "gnmi", "gnoi_client",
                        f"-target={dpu_ip}:{port}",
                        "-logtostderr", "-notls",
                        "-module", "System",
                        "-rpc", "RebootStatus"
                    ]
                    returncode, stdout, stderr = execute_gnoi_command(status_cmd)
                    if returncode == 0 and "reboot complete" in stdout.lower():
                        reboot_successful = True
                        break
                    time.sleep(interval)
                    elapsed += interval

                if reboot_successful:
                    logger.log_info(f"Reboot completed successfully for {dpu_name}.")
                else:
                    logger.log_warning(f"Reboot status polling timed out for {dpu_name}.")

                db.set("STATE_DB", key, {
                    "state_transition_in_progress": "False",
                    "transition_type": "none"
                })

        time.sleep(1)

if __name__ == "__main__":
    main()
