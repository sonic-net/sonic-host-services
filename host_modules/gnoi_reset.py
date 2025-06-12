"""gNOI reset module which performs factory reset."""

import json
import logging
import threading
import time
from host_modules import host_service
from host_modules import infra_host

MOD_NAME = "gnoi_reset"

# We don't execute any boot install commands to the non-switch-linux switches
# because they don't have boot count as the switch-linux switches do.
EXECUTE_BOOT_INSTALL_COMMAND = ""
GET_BOOT_INSTALL_VALUE_COMMAND = ""
EXECUTE_CLEANUP_COMMAND = []

# Timeout for SONiC Host Service to be killed during reboot. After executing the
# reboot command, we will wait for 260 seconds for the reboot to complete, where
# we expect that SONiC Host Service will be killed during this waiting period if
# the reboot is successful. If this module is still alive after the waiting
# period, we can conclude that the reboot has failed. Each container can take up
# to 20 seconds to get killed. In total, there are 10 containers, and adding a
# buffer of 1 minute brings up the delay value to be 260 seconds.
REBOOT_TIMEOUT = 260

EXECUTE_COLD_REBOOT_COMMAND = "sudo reboot"

logger = logging.getLogger(__name__)


class GnoiReset(host_service.HostModule):
  """DBus endpoint that executes the factory reset and returns the reset

  status and response.
  """

  def __init__(self, mod_name):
    self.lock = threading.Lock()
    self.is_reset_ongoing = False
    self.reset_request = {}
    self.reset_response = {}
    super(GnoiReset, self).__init__(mod_name)

  def populate_reset_response(
      self,
      reset_success=True,
      factory_os_unsupported=False,
      zero_fill_unsupported=False,
      detail="",
  ) -> tuple[int, str]:
    """Populate the factory reset response.

    Args:
        reset_success: A boolean type variable to indicate whether the factory
          reset succeeds or not.
        factory_os_unsupported: A boolean type variable to indicate whether the
          restoring to factory_os succeeds or not.
        zero_fill_unsupported: A boolean type variable to indicate whether the
          request to zero fill succeeds or not.
        detail: A string indicates the detailed error message of the factory
          reset if the error is not either factory_os_unsupported or
          zero_fill_unsupported.

    Returns:
        A integer that indicates whether the factory reset succeeds or not,
        and a json-style of StartResponse protobuf defined in reset.proto.
        The integer value will be 0 if the factory reset succeeds, or 1 if
        there is any failure happens.

        Examples of the return value:
            (0, dbus.String('{"reset_success": {}}'))
            (1, dbus.String('{
                    "reset_error": {
                        "other": true,
                        "detail": "Previous reset is ongoing."
                    }
                }')
            )
    """
    self.lock.acquire()
    self.reset_response = {}
    if reset_success:
      self.reset_response["reset_success"] = {}
    else:
      self.reset_response["reset_error"] = {}
      if factory_os_unsupported:
        self.reset_response["reset_error"]["factory_os_unsupported"] = True
      elif zero_fill_unsupported:
        self.reset_response["reset_error"]["zero_fill_unsupported"] = True
      else:
        self.reset_response["reset_error"]["other"] = True
      self.reset_response["reset_error"]["detail"] = detail
    response_data = json.dumps(self.reset_response)
    self.lock.release()
    return 0 if reset_success else 1, response_data

  def execute_reboot(self) -> None:
    """Execute cold reboot and log the error.

    when the reboot fails.
    """
    rc, stdout, stderr = infra_host.InfraHost._run_command(
        EXECUTE_COLD_REBOOT_COMMAND
    )
    if rc:
      logger.error(
          "%s: Cold reboot failed execution with stdout: %s, stderr: %s.",
          MOD_NAME,
          stdout,
          stderr,
      )
      return

    time.sleep(REBOOT_TIMEOUT)
    return

  def _check_reboot_in_progress(self) -> tuple[int, str]:
    """Checks if reboot is already in progress.

    Returns:
        A integer that indicates whether the factory reset succeeds or not,
        and a json-style of StartResponse protobuf defined in reset.proto.
        The integer value will be 0 if the factory reset succeeds, or 1 if
        there is any failure happens.

        Examples of the return value:
            (0, dbus.String('{"reset_success": {}}'))
            (1, dbus.String('{
                    "reset_error": {
                        "other": true,
                        "detail": "Previous reset is ongoing."
                    }
                }')
            )
    """
    self.lock.acquire()
    is_reset_ongoing = self.is_reset_ongoing
    self.lock.release()

    rc, stdout, stderr = infra_host.InfraHost._run_command(
        GET_BOOT_INSTALL_VALUE_COMMAND
    )
    if rc or not stdout:
      logger.error(
          "%s: Failed to get boot install value with stdout: %s, stderr: %s",
          MOD_NAME,
          stdout,
          stderr,
      )
      self.is_reset_ongoing = False
      return self.populate_reset_response(
          reset_success=False, detail="Failed to get the boot install value."
      )

    # Example of a valid google-specific platform stdout here is:
    # ["regionselect=a", "bootcount=0", "bootinstall=0"].
    boot_install = 0
    try:
      boot_install = int(stdout[2].split("=")[1])
    except (ValueError, IndexError) as error:
      return self.populate_reset_response(
          reset_success=False,
          detail="Failed to get the boot install value with error: %s."
          % str(error),
      )

    # Return without issuing the reset if the previous reset is ongoing.
    if is_reset_ongoing or boot_install != 0:
      return self.populate_reset_response(
        reset_success=False, detail="Previous reset is ongoing."
      )

    return 0, ""

  def _parse_arguments(self, options) -> tuple[int, str]:
    """Parses and validates the given arguments into a reset request.

    Args:
        options: A json-style string of StartRequest protobuf defined in
          factory_reset/reset.proto.

    Returns:
        A integer that indicates whether the factory reset succeeds or not,
        and a json-style of StartResponse protobuf defined in reset.proto.
        The integer value will be 0 if the factory reset succeeds, or 1 if
        there is any failure happens.

        Examples of the return value:
            (0, dbus.String('{"reset_success": {}}'))
            (1, dbus.String('{
                    "reset_error": {
                        "other": true,
                        "detail": "Previous reset is ongoing."
                    }
                }')
            )
    """
    self.reset_request = {}
    try:
      self.reset_request = json.loads(options)
    except ValueError:
      return self.populate_reset_response(
          reset_success=False,
          detail=(
              "Failed to parse json formatted factory reset request "
              "into python dict."
          ),
      )

    # Reject the request if zero_fill is set.
    if "zeroFill" in self.reset_request and self.reset_request["zeroFill"]:
      return self.populate_reset_response(
          reset_success=False,
          zero_fill_unsupported=True,
          detail="zero_fill operation is currently unsupported.",
      )

    # Issue a warning if retain_certs is set.
    if "retainCerts" in self.reset_request and self.reset_request["retainCerts"]:
      logger.warning("%s: retain_certs is currently ignored.", MOD_NAME)

    return 0, ""

  def _cleanup_images(self) -> None:
    """Cleans up the installed images, preparing for a factory reset."""
    logger.info("Cleaning up install images.")
    # Cleanup all install artifacts.
    for command in EXECUTE_CLEANUP_COMMAND:
      rc, stdout, stderr = infra_host.InfraHost._run_command(command)
      if rc:
        # Cleaning up artifacts is best effort, so continue on failure.
        logger.warning(
            "%s: Command %s execution failed with stdout: %s, stderr: %s.",
            MOD_NAME,
            command,
            stdout,
            stderr,
        )

  def _execute_reboot(self) -> tuple[int, str]:
    """Performs a cold reboot, putting the switch into boot install mode.

    Returns
        A integer that indicates whether the factory reset succeeds or not,
        and a json-style of StartResponse protobuf defined in reset.proto.
        The integer value will be 0 if the factory reset succeeds, or 1 if
        there is any failure happens.

        Examples of the return value:
            (0, dbus.String('{"reset_success": {}}'))
            (1, dbus.String('{
                    "reset_error": {
                        "other": true,
                        "detail": "Previous reset is ongoing."
                    }
                }')
            )

    Raises:
        RuntimeError: An error occurred when starting a new thread.
    """
    # Issue the boot install command.
    rc, stdout, stderr = infra_host.InfraHost._run_command(
        EXECUTE_BOOT_INSTALL_COMMAND
    )
    if rc:
      logger.error(
          "%s: Boot count execution with stdout: %s, stderr: %s.",
          MOD_NAME,
          stdout,
          stderr,
      )
      self.is_reset_ongoing = False
      return self.populate_reset_response(
          reset_success=False, detail="Boot count execution failed."
      )

    # Issue a cold reboot in a new thread and clear the reset response if
    # the reboot succeeds.
    try:
      t = threading.Thread(target=self.execute_reboot)
      t.start()
    except RuntimeError as error:
      self.is_reset_ongoing = False
      return self.populate_reset_response(
          reset_success=False,
          detail="Failed to start thread to execute reboot.",
      )

    return 0, ""

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is"
  )
  def issue_reset(self, options) -> tuple[int, str]:
    """Issues the factory reset by performing the following steps

    sequentially:
        1. Checks that there is no other reset requests ongoing.
        2. Issues a bootcount command to the switch if it runs switch-linux.
        3. Issues the cold reboot command to the switch.

    Args:
        options: A json-style string of StartRequest protobuf defined in
          factory_reset/reset.proto.

    Returns:
        A integer that indicates whether the factory reset succeeds or not,
        and a json-style of StartResponse protobuf defined in reset.proto.
        The integer value will be 0 always regardless of success or failure
        to ensure that the FE consumes the response correctly.

        Examples of the return value:
            (0, dbus.String('{"reset_success": {}}'))
            (0, dbus.String('{
                    "reset_error": {
                        "other": true,
                        "detail": "Previous reset is ongoing."
                    }
                }')
            )

    Raises:
        RuntimeError: An error occurred when starting a new thread.
    """
    # Override the error code to always note success, so that the FE consumes
    # the response correctly.
    print("Issueing reset from Back end")
    rc, resp = self._parse_arguments(options)
    if rc:
      return 0, resp

    rc, resp = self._check_reboot_in_progress()
    if rc:
      return 0, resp

    self.is_reset_ongoing = True
    if "factoryOs" in self.reset_request and self.reset_request["factoryOs"]:
      self._cleanup_images()

    rc, resp = self._execute_reboot()
    if rc:
      return 0, resp

    return 0, self.populate_reset_response()[1]


def register():
  """Return the class name"""
  return GnoiReset, MOD_NAME
