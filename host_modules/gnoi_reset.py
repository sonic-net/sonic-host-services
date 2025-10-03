"""gNOI reset module which performs factory reset."""

import json
import logging
import threading
import time
from host_modules import host_service
from host_modules.reboot import Reboot

MOD_NAME = "gnoi_reset"

logger = logging.getLogger(__name__)


class GnoiReset(host_service.HostModule):
    """DBus endpoint that executes the factory reset and returns the reset status and response."""

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
        """
        with self.lock:
            self.reset_response = {}
            response = {}
            if reset_success:
                self.reset_response["reset_success"] = {}
                response["reset_success"] = {}
            else:
                self.reset_response["reset_error"] = {}
                response["reset_error"] = {}
                if factory_os_unsupported:
                    self.reset_response["reset_error"]["factory_os_unsupported"] = True
                elif zero_fill_unsupported:
                    self.reset_response["reset_error"]["zero_fill_unsupported"] = True
                else:
                    self.reset_response["reset_error"]["other"] = True
                response["reset_error"]["detail"] = detail
            response_data = json.dumps(response)
        return 0, response_data

    def _check_reboot_in_progress(self) -> int:
        """Checks if reboot is already in progress."""
        if self.is_reset_ongoing:
            return 1
        else:
            return 0

    def _parse_arguments(self, options) -> tuple[int, str]:
        """Parses and validates the given arguments into a reset request."""
        try:
            raw = json.loads(options)
        except ValueError as e:
            logger.error("[%s]:Failed to parse factory reset request: %s", MOD_NAME, str(e))
            return self.populate_reset_response(
                reset_success=False,
                detail="Failed to parse json formatted factory reset request into python dict.",
            )

        # Normalize: support both camelCase and snake_case
        self.reset_request = {
            "factoryOs": raw.get("factoryOs", raw.get("factory_os", False)),
            "zeroFill": raw.get("zeroFill", raw.get("zero_fill", False)),
            "retainCerts": raw.get("retainCerts", raw.get("retain_certs", False)),
        }

        # Reject the request if zero_fill is set.
        if self.reset_request["factoryOs"] and self.reset_request["zeroFill"]:
            return self.populate_reset_response(
                reset_success=False,
                zero_fill_unsupported=True,
                detail="zero_fill operation is currently unsupported.",
            )
        # Issue a warning if retain_certs is set.
        if self.reset_request["factoryOs"] and self.reset_request["retainCerts"]:
            logger.warning("%s: retain_certs is currently ignored.", MOD_NAME)
            return self.populate_reset_response(
                reset_success=False,
                detail="Method FactoryReset.Start is currently unsupported."
            )
        # Reject the request if factoryOs is set. As the method is currently unsupported 
        if self.reset_request["factoryOs"]:
            return self.populate_reset_response(
                reset_success=False,
                detail="Method FactoryReset.Start is currently unsupported."
            )

        # Default fallback if no valid options triggered any action
        return self.populate_reset_response(
            reset_success=False,
            detail="Method FactoryReset.Start is currently unsupported."
        )

    def _execute_reboot(self) -> int:
        try:
            r = Reboot("reboot")
            t = threading.Thread(target=r.execute_reboot, args=("COLD",))
            t.start()
        except RuntimeError:
            self.is_reset_ongoing = False
            return 1

        return 0

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is"
    )

    def issue_reset(self, options) -> tuple[int, str]:
        """Issues the factory reset."""
        print("Issuing reset from back end")

        rc, resp = self._parse_arguments(options)
        if not rc:
            return rc, resp

        rc = self._check_reboot_in_progress()
        if rc:
            return self.populate_reset_response(reset_success=False, detail="Previous reset is ongoing.")

        self.is_reset_ongoing = True

        rc, resp = self._execute_reboot()
        if rc:
            return self.populate_reset_response(reset_success=False,detail="Failed to start thread to execute reboot.")

        # Default fallback if no valid options triggered any action
        return self.populate_reset_response(
            reset_success=False,
            detail="Method FactoryReset.Start is currently unsupported."
        )


def register():
    """Return the class name"""
    return GnoiReset, MOD_NAME
