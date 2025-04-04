"""reboot module which performs reboot"""

import json
import logging
import threading
import time
import docker
import psutil
from host_modules import host_service
from utils.run_cmd import _run_command

MOD_NAME = 'reboot'
# Reboot method in reboot request
# Both enum and string representations are supported
# Define an Enum for Reboot Methods which are defined as in https://github.com/openconfig/gnoi/blob/main/system/system.pb.go#L27
REBOOT_METHOD_COLD_BOOT_VALUES = {1, "COLD"}
REBOOT_METHOD_HALT_BOOT_VALUES = {3, "HALT"}
REBOOT_METHOD_WARM_BOOT_VALUES = {4, "WARM"}

# Timeout for SONiC Host Service to be killed during reboot
REBOOT_TIMEOUT = 260
HALT_TIMEOUT = 60

EXECUTE_COLD_REBOOT_COMMAND = "sudo reboot"
EXECUTE_HALT_REBOOT_COMMAND = "sudo reboot -p"
EXECUTE_WARM_REBOOT_COMMAND = "sudo warm-reboot"

logger = logging.getLogger(__name__)


class Reboot(host_service.HostModule):
    """DBus endpoint that executes the reboot and returns the reboot status
    """

    def __init__(self, mod_name):
        """Use threading.lock mechanism to read/write into response_data
           since response_data can be read/write by multiple threads"""
        self.lock = threading.Lock()
        # reboot_status_flag is used to keep track of reboot status on host
        self.reboot_status_flag = {}
        # Populating with default value i.e., no active reboot
        self.populate_reboot_status_flag()
        super(Reboot, self).__init__(mod_name)

    def populate_reboot_status_flag(self, active = False, when = 0, reason = ""):
        """Populates the reboot_status_flag with given input params"""
        self.lock.acquire()
        self.reboot_status_flag["active"] = active
        self.reboot_status_flag["when"] = when
        self.reboot_status_flag["reason"] = reason
        self.lock.release()
        return

    def validate_reboot_request(self, reboot_request):
        # Check whether reboot method is present.
        if "method" not in reboot_request:
            return 1, "Reboot request must contain a reboot method"

        # Check whether reboot method is valid.
        reboot_method = reboot_request["method"]
        valid_reboot_method = REBOOT_METHOD_COLD_BOOT_VALUES | REBOOT_METHOD_HALT_BOOT_VALUES | REBOOT_METHOD_WARM_BOOT_VALUES
        if reboot_method not in valid_reboot_method:
            return 1, "Unsupported reboot method: " + str(reboot_method)

        # Check whether delay is non-zero. delay key will not exist in reboot_request if it is zero
        if "delay" in reboot_request and reboot_request["delay"] != 0:
            return 1, "Delayed reboot is not supported"
        return 0, ""

    def is_container_running(self, container_name):
        """Check if a given container is running using the Docker SDK."""
        try:
            client = docker.from_env()
            containers = client.containers.list(filters={"name": container_name})

            for container in containers:
                if container.name == container_name and container.status == "running":
                    return True
            return False
        except Exception as e:
            logger.error("%s: Error checking container status for %s: [%s]", MOD_NAME, container_name, str(e))
            return False

    def is_halt_command_running(self):
        """Check if the halt command is running"""
        try:
            for process in psutil.process_iter(['cmdline']):
                if process.info['cmdline'] and "reboot" in process.info['cmdline'] and "-p" in process.info['cmdline']:
                    return True
            return False
        except Exception as e:
            logger.error("%s: Error checking if halt command is running: [%s]", MOD_NAME, str(e))
            return False

    def execute_reboot(self, reboot_method):
        """Executes reboot command based on the reboot_method initialised 
           and reset reboot_status_flag when reboot fails."""

        if reboot_method in REBOOT_METHOD_COLD_BOOT_VALUES:
            command = EXECUTE_COLD_REBOOT_COMMAND
            logger.warning("%s: Issuing cold reboot", MOD_NAME)
        elif reboot_method in REBOOT_METHOD_HALT_BOOT_VALUES:
            command = EXECUTE_HALT_REBOOT_COMMAND
            logger.warning("%s: Issuing halt reboot", MOD_NAME)
        elif reboot_method in REBOOT_METHOD_WARM_BOOT_VALUES:
            command = EXECUTE_WARM_REBOOT_COMMAND
            logger.warning("%s: Issuing WARM reboot", MOD_NAME)
        else:
            logger.error("%s: Unsupported reboot method: %d", MOD_NAME, reboot_method)
            return

        rc, stdout, stderr = _run_command(command)
        if rc:
            self.populate_reboot_status_flag()
            logger.error("%s: Reboot failed execution with stdout: %s, "
                         "stderr: %s", MOD_NAME, stdout, stderr)
            return
        
        """wait for the reboot to complete. Here, we expect that SONiC Host Service
           will be killed during this waiting period if the reboot is successful. If this module
           is still alive after the below waiting period, we can conclude that the reboot has failed.
           Each container can take up to 20 seconds to get killed. In total, there are 10 containers,
           and adding a buffer of 1 minute brings up the delay value.
           For Halt reboot_method, wait for 60 secs timeout. we expect pmon, syncd containers are killed, 
           if Halt reboot is Successful."""
        if reboot_method in REBOOT_METHOD_HALT_BOOT_VALUES:
            # Periodically check every 5 seconds until PMON container is stopped or timeout occurs
            logger.info("%s: Waiting until services are halted or timeout occurs", MOD_NAME)
            timeout = HALT_TIMEOUT
            start_time = time.monotonic()
            while time.monotonic() - start_time < timeout:
                if not self.is_halt_command_running() and not self.is_container_running("pmon"):
                    logger.info("%s: Halting the services is completed on the device", MOD_NAME)
                    return
                time.sleep(5)

            # Check if PMON container is still running after timeout
            if self.is_halt_command_running() or self.is_container_running("pmon"):
                #Halt reboot has failed, as pmon is still running.
                logger.error("%s: HALT reboot failed: Services are still running", MOD_NAME)
                self.populate_reboot_status_flag()
                return
            else:
                logger.info("%s: Halting the services is completed on the device", MOD_NAME)

        else:
            time.sleep(REBOOT_TIMEOUT)
            # Conclude that the reboot has failed if we reach this point
            self.populate_reboot_status_flag()
            return

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')

    def issue_reboot(self, options):
        """Initializes reboot thorugh RPC based on the reboot flag assigned.
           Issues reboot after performing the following steps sequentially:
          1. Checks that reboot_status_flag is not set
          2. Validates the reboot request
          3. Sets the reboot_status_flag
          4. Issues the reboot in a separate thread
        """
        logger.warning("%s: issue_reboot rpc called", MOD_NAME)
        self.lock.acquire()
        is_reboot_ongoing = self.reboot_status_flag["active"]
        self.lock.release()
        # Return without issuing the reboot if the previous reboot is ongoing
        if is_reboot_ongoing:
            return 1, "Previous reboot is ongoing"

        """Convert input json formatted reboot request into python dict.
           reboot_request is a python dict with the following keys:
               method - specifies the method of reboot
               delay - delay to issue reboot, key exists only if it is non-zero
               message - reason for reboot
               force - either true/false, key exists only if it is true
        """
        try:
            reboot_request = json.loads(options[0])
        except ValueError:
            return 1, "Failed to parse json formatted reboot request into python dict"

        # Validate reboot request
        err, errstr = self.validate_reboot_request(reboot_request)
        if err:
            return err, errstr

        # Sets reboot_status_flag to be in active state
        self.populate_reboot_status_flag(True, int(time.time()), reboot_request["message"])

        # Issue reboot in a new thread and reset the reboot_status_flag if the reboot fails
        try:
            t = threading.Thread(target=self.execute_reboot, args=(reboot_request["method"],))
            t.start()
        except RuntimeError as error:
            return 1, "Failed to start thread to execute reboot with error: " + str(error)
        return 0, "Successfully issued reboot"

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='', out_signature='is')
    def get_reboot_status(self):
        """Returns current reboot status on host in json format"""
        self.lock.acquire()
        response_data = json.dumps(self.reboot_status_flag)
        self.lock.release()
        return 0, response_data

def register():
    """Return the class name"""
    return Reboot, MOD_NAME
