#!/usr/bin/env python3
"""Initialize platform-specific gNMI CONFIG_DB and STATE_DB paths."""

from collections.abc import Mapping, MutableMapping, Sequence
import logging
import os
import shutil
import subprocess
import sys
import redis

ACTIVE = "active"
BIOS_VERSION_FILE = "/sys/devices/virtual/dmi/id/bios_version"
BOOT_LOADER_LC = "boot_loader"
BOOT_LOADER_UC = "BOOT_LOADER"
BUILD_VERSION = "build_version"
CHASSIS = "chassis"
CHASSIS_INFO = "CHASSIS_INFO|chassis"
CONFIG_DB = "CONFIG_DB"
CPU_TYPE = "cpu-type"
DEVICE_METADATA = "DEVICE_METADATA|localhost"
DISABLED = "disabled"
FW_SECURITY_INFO_FMT = "FW_SECURITY_INFO|%s"
FW_VERSION = "firmware-version"
FULLY_QUALIFIED_NAME = "fully-qualified-name"
PINS_PLATFORM_INIT_TBL = "PINS_PLATFORM_INIT"
PINS_PLATFORM_READY = "ready"
HARDWARE_VERSION = "hardware-version"
HOST = "host"
HOSTNAME = "hostname"
HOST_STATS = "HOST_STATS|HOSTNAME"
INACTIVE = "inactive"
KERNEL = "kernel_version"
MODULE_TYPE = "module-type"
NAME = "name"
NETWORK_STACK0 = "network_stack0"
NETWORK_STACK = "network_stack"
AVAILABLE = "Available"
CURRENT = "Current"
NEXT = "Next"
NOT_APPLICABLE = "N/A"
NULL = "NULL"
OPER_STATUS = "oper-status"
OPERATING_SYSTEM = "OPERATING_SYSTEM"
OTHER_ROOT = "/host"
OS0 = "os0"
OS1 = "os1"
PARENT = "parent"
PAYLOAD_SIGNATURE_TYPE = "payload-signature-type"
PAYLOAD_VERSION = "payload-version"
PLATFORM = "platform"
PORT_TABLE_CPU = "PORT_TABLE:CPU"
REDIS_APPL_STATE_DB_NUMBER = 14
REDIS_CONFIG_DB_NUMBER = 4
REDIS_HOST = "localhost"
REDIS_PORT_NUMBER = 6379
REDIS_STATE_DB_NUMBER = 6
RELEASE = "RELEASE"
SECURE_PAYLOAD_ENFORCED = "secure-payload-enforced"
SERIAL_NUMBER = "serial-no"
SOFTWARE_MODULE = "SOFTWARE_MODULE"
SOFTWARE_VERSION = "software-version"
STATE_DB = "STATE_DB"
SW_COMP_INFO_BOOTLOADER = f"SW_COMP_INFO|{BOOT_LOADER_LC}"
SW_COMP_INFO_OS0 = f"SW_COMP_INFO|{OS0}"
SW_COMP_INFO_STACK0 = f"SW_COMP_INFO|{NETWORK_STACK0}"
SYSLOG_CONF = "/etc/rsyslog.conf"
SYSLOG_SERVER = "SYSLOG_SERVER"
TYPE = "type"
USERSPACE_PACKAGE_BUNDLE = "USERSPACE_PACKAGE_BUNDLE"
VERSION_FILE = "/etc/sonic/sonic_version.yml"
SQUASHFS_EXTRACT_PATH = "/tmp/fs"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PinsPlatformInit:
  """Initialize platform-specific gNMI CONFIG_DB and STATE_DB paths."""

  def __init__(self):
    try:
      self.config_db = redis.Redis(
          host=REDIS_HOST, port=REDIS_PORT_NUMBER, db=REDIS_CONFIG_DB_NUMBER
      )
      self.state_db = redis.Redis(
          host=REDIS_HOST, port=REDIS_PORT_NUMBER, db=REDIS_STATE_DB_NUMBER
      )
      self.appl_state_db = redis.Redis(
          host=REDIS_HOST, port=REDIS_PORT_NUMBER, db=REDIS_APPL_STATE_DB_NUMBER
      )
    except redis.RedisError as error:
      logger.error("Error interacting with Redis: %s", str(error))
      sys.exit(1)

  def populate_version_dictionaries(self):
    """Populate the version dictionaries for the primary and alternate versions."""
    self.primary_version_dict = self.populate_version_dictionary(VERSION_FILE)
    self.alternate_versions_dict = {}
    current_image = self.get_network_stacks(CURRENT)
    available_images = self.get_network_stacks(AVAILABLE)
    alternate_version_file = SQUASHFS_EXTRACT_PATH + VERSION_FILE
    for image in available_images:
      if image not in current_image:
        # Extract the version file from the squashfs image.
        # unsquashfs -no-progress-output -quiet -force -d <dest_path>
        #            <squash file path> <file to extract>
        os.makedirs(SQUASHFS_EXTRACT_PATH, exist_ok=True)
        # TODO(b/519279593): Remove this when we fix sonic-installer for systemd
        # to not prepend the slot name.
        img = image.split(":", maxsplit=1)[-1]
        self.run_command([
            "unsquashfs",
            "-n",
            "-q",
            "-f",
            "-d",
            SQUASHFS_EXTRACT_PATH,
            f"/host/image-{img}/fs.squashfs",
            f"{VERSION_FILE}",
        ])
        if os.path.exists(alternate_version_file):
          self.alternate_versions_dict[image] = (
              self.populate_version_dictionary(alternate_version_file)
          )
        shutil.rmtree(SQUASHFS_EXTRACT_PATH)

  def run_command(self, command_as_list: Sequence[str]) -> str:
    """Runs a host platform command and returns the capture of STDOUT.

    Args:
      command_as_list: The shell command to execute.

    Returns:
      The shell command result captured from STDOUT.

    Raises:
      CalledProcessError: If the command does not execute successfully.
    """
    p = subprocess.run(
        command_as_list,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = p.stdout.decode("utf-8").strip()
    return output

  def get_network_stacks(self, type_of_stack: str) -> Sequence[str]:
    """Get the current, next, or available network stacks.

    This is done by parsing the output from the sonic-installer list command.
    Current:
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_gnmi_2800-55b3ddcb
    Next:
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_gnmi_2800-55b3ddcb
    Available:
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_gnmi_2800-55b3ddcb
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3220-0db41e76

    Args:
      type_of_stack: The type of stack to get, e.g. CURRENT, NEXT, or AVAILABLE.

    Returns:
      A list of network stack names.
    """
    output = self.run_command(["sonic-installer", "list"])
    include_lines = False
    image_names = []
    for line in output.split("\n"):
      # Remove the "SONiC-OS-" prefix from the image name.
      line = line.replace("SONiC-OS-", "").strip()
      if include_lines and line:
        image_names.append(line)
      elif line.startswith(f"{type_of_stack}:"):
        line = line.split(":")[1]
        # Look for current or next image.
        # Current: SONiC-OS-image1
        # Next: SONiC-OS-image2
        if type_of_stack == CURRENT or type_of_stack == NEXT:
          return [line.strip()]
        # Look for Available and include all images after that line.
        elif type_of_stack == AVAILABLE:
          include_lines = True
    return image_names

  def write_chassis_info_paths(self) -> None:
    """Write chassis elements to STATE_DB: CHASSIS_INFO|chassis.

    Paths written include:
      /components/component[name=<chassis>]/chassis/state/platform
      /components/component[name=<chassis>]/state/firmware-version
      /components/component[name=<chassis>]/state/fully-qualified-name
    """
    board_type = str(self.config_db.hget(DEVICE_METADATA, "platform"), "utf-8")
# copybara:strip_begin(arista)
    if board_type.startswith("x86_64-arista_7060xe7_64p"):
      self.platform_name = "banff"
      self.state_db.hset(CHASSIS_INFO, PLATFORM, self.platform_name)
# copybara:strip_end
    # Version information.
    if BUILD_VERSION in self.primary_version_dict:
      self.state_db.hset(
          CHASSIS_INFO, FW_VERSION, self.primary_version_dict[BUILD_VERSION]
      )
      logger.info(
          f"Installed image is {self.primary_version_dict[BUILD_VERSION]}"
      )
    else:
      logger.error(f"Unable to find '{BUILD_VERSION}' in {VERSION_FILE}")

  def populate_version_dictionary(
      self, file_name: str, separator: str = ":"
  ) -> Mapping[str, str]:
    """Reads version file to create attribute dictionary.

    The file is structed as one key-value per line, separated by a separator
    arg.
    An example file looks would look like this:
    build_version:
    'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_2817-04b87ee8'
    debian_version: '11.11'
    kernel_version: '5.10.0-30-2-amd64'
    asic_type: cisco-8000
    asic_subtype: 'cisco-8000'
    commit_id: '04b87ee8'
    branch: 'HEAD'
    release: '202305'
    build_date: Sat May  3 02:25:31 UTC 2025
    build_number: pins/sonic-buildimage/gcp_ubuntu/presubmit_cisco/2817
    built_by: kbuilder@kokoro-gcp-ubuntu-prod-972425314

    Arguments: None.

    Returns:
      A dictionary mapping strings representing attribute names to strings
      representing the values.
    """
    rv: MutableMapping[str, str] = {}
    try:
      with open(file_name, "r") as f:
        for line in f:
          if separator not in line:
            continue
          k, v = line.split(separator, maxsplit=1)
          rv[k.strip()] = v.strip().strip("'")
    except OSError:
      logger.error("Unable to read file: %s", file_name)
    except ValueError:
      logger.error("File is not formatted as expected: %s", file_name)
    return rv

  def write_sw_bootloader_paths(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|boot_loader.

    Paths written include:
    /components/component[name=<bootloader>]/software-module/state/module-type
    /components/component[name=<bootloader>]/state/name
    /components/component[name=<bootloader>]/state/parent
    /components/component[name=<bootloader>]/state/software-version
    /components/component[name=<bootloader>]/state/type

    Arguments: None.
    """
    self.state_db.hset(SW_COMP_INFO_BOOTLOADER, NAME, BOOT_LOADER_LC)
    self.state_db.hset(SW_COMP_INFO_BOOTLOADER, PARENT, CHASSIS)
    bios_version = self.run_command(["cat", BIOS_VERSION_FILE])
    self.state_db.hset(SW_COMP_INFO_BOOTLOADER, SOFTWARE_VERSION, bios_version)
    self.state_db.hset(SW_COMP_INFO_BOOTLOADER, TYPE, BOOT_LOADER_UC)

  def populate_active_network_stack(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|network_stack0

    Paths written include:
      STATE_DB SW_COMP_INFO|network_stack0
      /components/component[name=<primary-network-stack>]/software-module/state/module-type
      /components/component[name=<primary-network-stack>]/state/name
      /components/component[name=<primary-network-stack>]/state/parent
      /components/component[name=<primary-network-stack>]/state/oper-status
      /components/component[name=<primary-network-stack>]/state/software-version
      /components/component[name=<primary-network-stack>]/state/type
    Note: We interpret primary-network-stack to mean the currently running
    network stack.  This means that the "oper-status" path for
    primary-network-stack will always be "active".
    As a result, the "oper-status" in the alternate-network-stacks path will
    always be the "inactive".

    Arguments: None.
    """
    # STATE_DB SW_COMP_INFO|network_stack0
    self.state_db.hset(
        SW_COMP_INFO_STACK0, MODULE_TYPE, USERSPACE_PACKAGE_BUNDLE
    )
    self.state_db.hset(SW_COMP_INFO_STACK0, NAME, NETWORK_STACK0)
    self.state_db.hset(SW_COMP_INFO_STACK0, PARENT, CHASSIS)
    self.state_db.hset(SW_COMP_INFO_STACK0, OPER_STATUS, ACTIVE)
    if BUILD_VERSION in self.primary_version_dict:
      self.state_db.hset(
          SW_COMP_INFO_STACK0,
          SOFTWARE_VERSION,
          self.primary_version_dict[BUILD_VERSION],
      )
    else:
      logger.error(f"Unable to find '{BUILD_VERSION}' in {VERSION_FILE}")
    self.state_db.hset(SW_COMP_INFO_STACK0, TYPE, SOFTWARE_MODULE)

  def populate_alternate_network_stacks(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|network_stack1/2/..

      STATE_DB SW_COMP_INFO|network_stack1/2/..
      /components/component[name=<alternate-network-stacks>]/software-module/state/module-type
      /components/component[name=<alternate-network-stacks>]/state/name
      /components/component[name=<alternate-network-stacks>]/state/parent
      /components/component[name=<alternate-network-stacks>]/state/oper-status
      /components/component[name=<alternate-network-stacks>]/state/software-version
      /components/component[name=<alternate-network-stacks>]/state/type

    Arguments: None.
    """
    for index, (image_name, image_dict) in enumerate(
        self.alternate_versions_dict.items(), start=1
    ):
      network_stack_name = f"network_stack{index}"
      component_name = f"SW_COMP_INFO|{network_stack_name}"
      # STATE_DB SW_COMP_INFO|network_stack1
      # STATE_DB SW_COMP_INFO|network_stack2
      # ...
      self.state_db.hset(component_name, MODULE_TYPE, USERSPACE_PACKAGE_BUNDLE)
      self.state_db.hset(component_name, NAME, network_stack_name)
      self.state_db.hset(component_name, PARENT, CHASSIS)
      self.state_db.hset(component_name, OPER_STATUS, INACTIVE)
      self.state_db.hset(component_name, TYPE, SOFTWARE_MODULE)
      if BUILD_VERSION in image_dict:
        self.state_db.hset(
            component_name,
            SOFTWARE_VERSION,
            image_dict[BUILD_VERSION],
        )
      else:
        logger.error(
            f"Unable to find '{BUILD_VERSION}' in {image_name}:{VERSION_FILE}"
        )

  def write_sw_network_stack_paths(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|network_stack0/1/2....

    Arguments: None.
    """
    self.populate_active_network_stack()
    self.populate_alternate_network_stacks()

  def populate_primary_os(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|os0.

    Paths written include:
      STATE_DB SW_COMP_INFO|os0
      /components/component[name=<primary-os>]/state/name
      /components/component[name=<primary-os>]/state/oper-status
      /components/component[name=<primary-os>]/state/parent
      /components/component[name=<primary-os>]/state/software-version
      /components/component[name=<primary-os>]/state/type

      Note: We interpret primary-os to mean the currently running OS. This means
      that the "oper-status" path for primary-os will always be "active".
      As a result, the "oper-status" in the alternate-os path will always
      be the "inactive".
    """
    # STATE_DB SW_COMP_INFO|os0
    if KERNEL in self.primary_version_dict:
      self.state_db.hset(SW_COMP_INFO_OS0, NAME, OS0)
      self.state_db.hset(SW_COMP_INFO_OS0, OPER_STATUS, ACTIVE)
      self.state_db.hset(SW_COMP_INFO_OS0, PARENT, CHASSIS)
      self.state_db.hset(
          SW_COMP_INFO_OS0, SOFTWARE_VERSION, self.primary_version_dict[KERNEL]
      )
      self.state_db.hset(SW_COMP_INFO_OS0, TYPE, OPERATING_SYSTEM)
    else:
      logger.error(f"Unable to find '{KERNEL}' in {VERSION_FILE}")

  def populate_alternate_os(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|os1/2..

    Paths written include:
    STATE_DB SW_COMP_INFO|os1
    /components/component[name=<alternate-os>]/state/name
    /components/component[name=<alternate-os>]/state/oper-status
    /components/component[name=<alternate-os>]/state/parent
    /components/component[name=<alternate-os>]/state/software-version
    /components/component[name=<alternate-os>]/state/type
    """
    # STATE_DB SW_COMP_INFO|os1
    for index, (image_name, image_dict) in enumerate(
        self.alternate_versions_dict.items(), start=1
    ):
      if KERNEL in image_dict:
        os_name = f"os{index}"
        component_name = f"SW_COMP_INFO|{os_name}"
        self.state_db.hset(component_name, NAME, os_name)
        self.state_db.hset(component_name, OPER_STATUS, INACTIVE)
        self.state_db.hset(component_name, PARENT, CHASSIS)
        self.state_db.hset(
            component_name,
            SOFTWARE_VERSION,
            image_dict[KERNEL],
        )
        self.state_db.hset(component_name, TYPE, OPERATING_SYSTEM)
      else:
        logger.error(
            f"Unable to find {image_name}:{VERSION_FILE} or '{KERNEL}' in"
            f" {image_name}:{VERSION_FILE}"
        )

  def write_sw_os_paths(self) -> None:
    """Write SW comp info elements to STATE_DB: SW_COMP_INFO|os0/1/2..."""
    self.populate_primary_os()
    self.populate_alternate_os()

  def write_system_paths(self) -> None:
    """Write SW comp info elements to STATE_DB: SYSLOG_SERVER|<ipaddress>.

    Paths written include:
    /system/logging/remote-servers/remote-server[host=<syslog-ip-address>]/state/host

    Arguments: None.
    """
    ip_found = False
    try:
      with open(SYSLOG_CONF, "r") as f:
        for line in f:
          if 'Target="' in line:
            # line is:  <target> <action> <ip> <the rest>
            words = line.split()
            for word in words:
              if word.startswith("Target="):
                ip = word.strip()
                # Remove grep pattern and trailing quote
                ip = ip[8:-1]
                self.state_db.hset(f"{SYSLOG_SERVER}|{ip}", HOST, ip)
                ip_found = True
    except OSError:
      logger.error(f"Unable to read {SYSLOG_CONF}")
    if not ip_found:
      logger.error(
          f"Unable to find IP address in {SYSLOG_CONF} file. Unable to extract"
          " IP address from line"
      )

  def get_network_name(self) -> str:
    """Gets the fully-qualified network name of the switch.

    We use the hostname command instead of hostname -f, since the hostname -f
    command times out in some IPv6 settings with unreachable name servers.
# copybara:strip_begin(internal bug ID)
    See b/193807557.
# copybara:strip_end

    Returns:
      The fully-qualified name of the switch, e.g. ju4u1m1.sqs02.net.google.com.
    """
    try:
      network_name = self.run_command(["hostname"])
      # We have inconsistencies in netconfig.state on the switch.  Sometimes the
      # fully-qualified name exists, and sometimes it doesn't.
      if not network_name.endswith(".net.google.com"):
        network_name += ".net.google.com"
    except subprocess.CalledProcessError as ce:
      network_name = f"hostname command failed - {str(ce)}"
    return network_name

  def write_host_paths(self) -> None:
    """Write SW comp info elements to CONFIG_DB and STATE_DB.

    Paths written include:
    CONFIG_DB DEVICE_METADATA|localhost
    /system/config/hostname
    STATE_DB HOST_STATS|HOSTNAME
    /system/state/hostname

    Arguments: None.
    """
    self.network_name = self.get_network_name()
    if self.config_db.hget(DEVICE_METADATA, HOSTNAME) is None:
      self.config_db.hset(DEVICE_METADATA, HOSTNAME, self.network_name)
    self.state_db.hset(HOST_STATS, HOSTNAME, self.network_name)
    self.state_db.hset(CHASSIS_INFO, FULLY_QUALIFIED_NAME, self.network_name)

  def write_cpu_path_appl_state_db(self) -> None:
    """Creates PORT_TABLE:CPU in APPL_STATE_DB.

    This is needed to be able to retrieve the path:

      /interfaces/interface[name=CPU]/state/counters
    """
    self.appl_state_db.hset(PORT_TABLE_CPU, "NULL", "NULL")

  def write_pinsinit_done(self) -> None:
    """Write PINS_INIT_DONE to STATE_DB."""
    # The last thing to do, indicating we are done initializing.
    self.state_db.hset(
        PINS_PLATFORM_INIT_TBL, PINS_PLATFORM_READY, PINS_PLATFORM_READY
    )


def main() -> None:
  """Write PINS platform specific init info to STATE/APPL_STATE_DB."""
  platform = PinsPlatformInit()
  platform.write_cpu_path_appl_state_db()
  platform.populate_version_dictionaries()
  platform.write_chassis_info_paths()
  platform.write_sw_bootloader_paths()
  platform.write_sw_network_stack_paths()
  platform.write_sw_os_paths()
  platform.write_system_paths()
# copybara:strip_begin(internal bug ID)
  # TODO(b/419394833: Enable after hostname is fixed.
  # platform.write_host_paths()
# copybara:strip_end
  platform.write_pinsinit_done()
# copybara:strip_begin(internal hardware codename)
  # write_firmware_info_paths()
# copybara:strip_end


if __name__ == "__main__":
  main()
