"""gNOI OS host service executes OS update and install requests.

   third_party/buzznik/sonic-telemetry/proto/gnoi/os/os.proto
"""

import enum
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

import host_service
import infra_host
import os_mgmt.gnoi_os_proto_defs as ospb
import redis
from swsscommon import swsscommon

MOD_NAME = 'gnoi_os_mgmt'

# The default directory OsUpdate protos are stored.
FE_TRANSFER_DIR = '/tmp/'

# The location of install manager.
INSTALL_MANAGER_LOCATION = 'install_manager'

# The output locations for install manager results.
ACTIVE_SIDE_OUTPUT_LOCATION = '/tmp/active_output.pb.txt'
INACTIVE_SIDE_OUTPUT_LOCATION = '/tmp/inactive_output.pb.txt'

# Commands to get the active and inactive image verisons
ACTIVE_SIDE_VERSION_COMMAND = ['sudo', 'sonic-installer', 'list']
ACTIVE_SIDE_REGEX = rb'Current:\s+SONiC-OS-(.*)'
INACTIVE_SIDE_VERSION_COMMAND = ['sudo', 'get-other-sonic-version']

# The parameters for connecting and writing to the state DB.
REDIS_HOST = 'localhost'
REDIS_PORT_NUMBER = 6379
REDIS_STATE_DB_NUMBER = 6
SW_COMP_INFO_STACK1 = 'SW_COMP_INFO|network_stack1'
SOFTWARE_VERSION = 'software-version'

# A set of invalid version strings
INVALID_VERSION_STRINGS = ['.', '..']

# Version prefix for stack rollback.
STACK_ROLLBACK_VERSION_PREFIX = 'rollback_'

# Stack rollback delay.
STACK_ROLLBACK_DELAY_SECONDS = 3

# Keys need to copy from active paration to inactive paration in rollback.
ACTIVE_PARTITION_DIR = '/mnt/region_config'
INACTIVE_PARTITION_DIR = '/mnt/other_config'
ROLLBACK_KEYS = [
    'etc/googlekeys/ssh_ca_pub_key', 'accounts.d/root/.ssh/authorized_keys',
    'accounts.d/root/.ssh/authorized_users', 'netconfig.state'
]

try:
    COMPONENT_STATE_MINOR = swsscommon.ComponentState_kMinor
except AttributeError:
    COMPONENT_STATE_MINOR = 1

logger = logging.getLogger(__name__)


class RunningStatus(enum.Enum):
  """Enum for InstallManager operation status.
  """

  NOT_STARTED = 0
  RUNNING = 1
  FINISHED = 2

class DummyComponentState:
    """Fallback when swsscommon StateHelperManager is not available."""
    def ReportComponentState(self, level, message):
        logging.warning(f"[DummyComponentState] {level}: {message}")

class InstallManagerThread(threading.Thread):
  """Thread wrapper for InstallManager subprocess.
  """

  def __init__(self, input_file: str, active_side: bool):
    super().__init__()
    self.status_lock = threading.Lock()
    self.status = RunningStatus.NOT_STARTED
    self.input_file = input_file
    self.active_side = active_side
    try:
        self.component_state = swsscommon.StateHelperManager_ComponentSingleton(
            swsscommon.SystemComponent_kHost)
    except AttributeError:
        logging.warning("StateHelperManager_ComponentSingleton not available; using dummy state reporter.")
        self.component_state = DummyComponentState()

  def run(self) -> None:
    with self.status_lock:
      self.status = RunningStatus.RUNNING
    logger.info('starting InstallManager')

    output_location = (
        ACTIVE_SIDE_OUTPUT_LOCATION
        if self.active_side else INACTIVE_SIDE_OUTPUT_LOCATION)
    im_command = ("%s --os_update_path='%s' --os_update_result_path='%s'" %
                  (INSTALL_MANAGER_LOCATION, self.input_file, output_location))

    rc, stdout, stderr = infra_host.InfraHost._run_command(im_command)
    if rc:
      err_str = (
          'InstallManager execution failed with stdout: {}, stderr: {}'.format(
              stdout, stderr))
      logger.error(err_str)

      # Raise alarm when install manager fails.
      self.component_state.ReportComponentState(
          COMPONENT_STATE_MINOR, err_str)
    else:
      logger.info('InstallManager done. rc: %d stdout:\n%s\nstderr:\n%s', rc,
                  '\n'.join(stdout), '\n'.join(stderr))
      standby_version = get_version(False)
      if not standby_version:
        standby_version = ''
      state_db = redis.Redis(
          host=REDIS_HOST, port=REDIS_PORT_NUMBER, db=REDIS_STATE_DB_NUMBER)
      state_db.hset(SW_COMP_INFO_STACK1, SOFTWARE_VERSION, standby_version)

    with self.status_lock:
      self.status = RunningStatus.FINISHED

  def is_finished(self):
    with self.status_lock:
      return self.status == RunningStatus.FINISHED


class StackRollbackThread(threading.Thread):
  """Thread for stack rollback.
  """

  def __init__(self, delay: int):
    super().__init__()
    self.delay = delay
    self.status_lock = threading.Lock()
    self.done = False

  @staticmethod
  def _run_command(cmd: str):
    proc = subprocess.Popen(
        cmd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True)
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr

  def run(self) -> None:
    # Wait for gNOI to return
    time.sleep(self.delay)
    logger.info('starting stack rollback')

    # Copy keys from active partition to inactive partition
    try:
      for key in ROLLBACK_KEYS:
        shutil.copyfile(
            os.path.join(ACTIVE_PARTITION_DIR, key),
            os.path.join(INACTIVE_PARTITION_DIR, key))
    except Exception as e:
      logger.error('Error in copying keys in stack rollback: %s', e)

    # Change partition and reboot.
    # We parse /proc/cmdline to get the active partition.
    command = ("/sbin/bootcount --setregion=$(/sbin/bootinfo region | "
               "sed 's/a/c/;s/b/a/;s/c/b/') && reboot")
    rc, stdout, stderr = self._run_command(command)
    if rc:
      logger.error('Stack rollback failed with stdout: %s, stderr: %s', stdout,
                   stderr)
    with self.status_lock:
      self.done = True

  def is_finished(self):
    with self.status_lock:
      return self.done


def _get_active_version() -> str:
  # The active side version command must parse the output of sonic-installer
  # list.
  # Ex:
  # Current: SONiC-OS-e2e.0-0cc34e6f8
  # Next: ONIE
  # Available:
  # SONiC-OS-e2e.0-0cc34e6f8
  # will return e2e.0-0cc34e6f8
  result = subprocess.run(
      ACTIVE_SIDE_VERSION_COMMAND, capture_output=True, check=False)
  if result.returncode:
    return ''
  for line in result.stdout.split(b'\n'):
    print(f"DEBUG: checking line: {line}")
    match = re.match(ACTIVE_SIDE_REGEX, line)
    if match:
      return str(match.group(1), encoding='utf-8')
  return ''


def _get_inactive_version() -> str:
  # The inactive side version command directly returns the inactive side
  # version in stdout.
  # Ex: e2e.0-0cc34e6f8
  result = subprocess.run(
      INACTIVE_SIDE_VERSION_COMMAND, capture_output=True, check=False)
  if result.returncode:
    return ''
  return str(result.stdout.strip(), encoding='utf-8')


def get_version(active_side: bool) -> Optional[str]:
  return _get_active_version() if active_side else _get_inactive_version()


class GnoiOsMgmt(host_service.HostModule):
  """DBus endpoint that manages install and update.
  """

  def __init__(self, mod_name):
    self.lock = threading.Lock()
    self.image_receipt_in_progress = False
    self.version_in_progress = None
    self.install_manager_thread = None
    self.stack_rollback_thread = None
    super(GnoiOsMgmt, self).__init__(mod_name)

  @staticmethod
  def _get_dict(json_input: str):
    """Converts json string to dict.

    Convert json formatted input string to a dict.

    Args:
      json_input: json formatted string
    Returns:
      A dict representation of python string.
      None if error.
    """

    try:
      request = json.loads(json_input)
    except json.JSONDecodeError:
      return None

    return request

  def _install_in_progress(self):
    return ((self.install_manager_thread and
             not self.install_manager_thread.is_finished()) or
            (self.stack_rollback_thread and
             not self.stack_rollback_thread.is_finished()))

  @staticmethod
  def _version_string_valid(version_string: str):
    if not version_string:
      return False
    elif version_string in INVALID_VERSION_STRINGS:
      return False
    elif '/' in version_string:
      return False
    return True

  @staticmethod
  def _unspecified_install_error(error_string: str):
    install_error = {}
    install_error[ospb.TYPE_FIELD] = ospb.INSTALL_ERROR_UNSPECIFIED
    install_error[ospb.DETAIL_FIELD] = error_string
    return json.dumps({ospb.INSTALL_ERROR_FIELD: install_error})

  def handle_transfer_start(self, install_request):
    logger.info('Received TransferRequest')

    # Throw out previous state
    if self.image_receipt_in_progress:
      logger.info('Discarding existing in progress transfer of version %s',
                  self.version_in_progress)
      self.image_receipt_in_progress = False
      self.version_in_progress = None

    if ospb.VERSION_FIELD not in install_request[ospb.TRANSFER_REQUEST_FIELD]:
      logger.error('TransfterStart missing version')
      return 0, self._unspecified_install_error(
          'TransferRequest missing version')

    self.version_in_progress = install_request[ospb.TRANSFER_REQUEST_FIELD][
        ospb.VERSION_FIELD]
    if not self._version_string_valid(self.version_in_progress):
      self.version_in_progress = None
      return 0, self._unspecified_install_error(
          'TransferRequest called with invalid version: '
          f'{self.version_in_progress}')
    self.image_receipt_in_progress = True

    target_filepath = os.path.join(FE_TRANSFER_DIR, self.version_in_progress)
    if os.path.exists(target_filepath):
      logger.info('Discarding existing file at destination %s', target_filepath)
      os.remove(target_filepath)

    return 0, json.dumps({ospb.TRANSFER_READY_FIELD: {}})

  def handle_transfer_end(self, install_request):
    logger.info('Received TransferEnd')

    if not self.image_receipt_in_progress:
      logger.error('TransferEnd called before transferRequest')
      return 0, self._unspecified_install_error(
          'TransferEnd called before transferRequest')

    target_filepath = os.path.join(FE_TRANSFER_DIR, self.version_in_progress)
    if not os.path.exists(target_filepath):
      logger.error('Target file %s does not exist', target_filepath)
      self.version_in_progress = None
      self.image_receipt_in_progress = False

      return 0, self._unspecified_install_error(
          'Target file %s does not exist' % target_filepath)

    output = json.dumps(
        {ospb.VALIDATED_FIELD: {
            ospb.VERSION_FIELD: self.version_in_progress
        }})
    self.version_in_progress = None
    self.image_receipt_in_progress = False
    return 0, output

  def stack_rollback(self, version: str):
    # Check active stack version
    active_version = get_version(True)
    if active_version and active_version == version:
      logger.info('Requested version %s is already running', version)
      return True

    # Check inactive stack version
    inactive_version = get_version(False)
    if inactive_version and inactive_version == version:
      logger.info('Rolling back to version %s', version)
      self.stack_rollback_thread = StackRollbackThread(
          STACK_ROLLBACK_DELAY_SECONDS)
      self.stack_rollback_thread.start()
      return True

    return False

  # 'as' = array of strings (reference dbus-python)
  # 'is' = integer, string
  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
  def install(self, options):
    """Begins or continues the staging of files for an install.

    Args:
      options: = string, json formatted InstallRequest from os.proto

    Returns:
      int: 0 on success, 1 on failure
      string: formatted InstallResponse from os.proto on success
              error string on failure
    """

    logger.info('Install called with json %s', options.replace('\n', ' '))
    with self.lock:
      install_request = self._get_dict(options)
      if not install_request:
        logger.info('start: rx\'d invalid json length: %d content: "%s"',
                    len(options), options.replace('\n', ' '))
        return 1, 'GnoiOsMgmt.install rx\'d invalid json'

      if self._install_in_progress():
        logger.error('Install in progress')
        return 0, json.dumps({
            ospb.INSTALL_ERROR_FIELD: {
                ospb.TYPE_FIELD: ospb.INSTALL_ERROR_INSTALL_IN_PROGRESS
            }
        })

      if ospb.TRANSFER_REQUEST_FIELD in install_request:
        result = self.handle_transfer_start(install_request)
        logger.info('response to FE: rc: %d str: %s', result[0], result[1])
        return result
      elif ospb.TRANSFER_CONTENT_FIELD in install_request:
        logger.error('BE should not receive transfer_content')
        self.version_in_progress = None
        self.image_receipt_in_progress = False

        return 0, self._unspecified_install_error(
            'BE should not receive transfer_content')
      elif ospb.TRANSFER_END_FIELD in install_request:
        result = self.handle_transfer_end(install_request)
        logger.info('response to FE: rc: %d str: %s', result[0], result[1])
        return result
      else:
        return 0, self._unspecified_install_error(
            'No data included in GnoiOsMgmt.install')

    return 1, 'Failed to acquire lock (this should be impossible)'

  # 'as' = array of strings (reference dbus-python)
  # 'is' = integer, string
  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
  def activate(self, options):
    """Attempts to activate a specified OS version on the device.

    Uses files staged in install.

    Args:
      options: = string, json formatted ActivateRequest from os.proto

    Returns:
      int: 0 on success, 1 on failure
      string: formatted ActivateResponse from os.proto on success
              error string on failure
    """

    logger.info('Activate called with json %s', options.replace('\n', ' '))
    with self.lock:
      activate_request = self._get_dict(options)
      if not activate_request:
        logger.error('start: rx\'d invalid json %s', options.replace('\n', ' '))
        return 1, 'GnoiOsMgmt.activate rx\'d invalid json'

      if self._install_in_progress():
        logger.error('Install in progress')
        activate_error = {}
        activate_error[ospb.TYPE_FIELD] = ospb.ACTIVATE_ERROR_UNSPECIFIED
        activate_error[ospb.DETAIL_FIELD] = ('Activation in progress')
        return 0, json.dumps({ospb.ACTIVATE_ERROR_FIELD: activate_error})

      if ospb.VERSION_FIELD not in activate_request:
        logger.error('GnoiOsMgmt.activate missing version')
        activate_error = {}
        activate_error[ospb.TYPE_FIELD] = ospb.ACTIVATE_ERROR_UNSPECIFIED
        activate_error[ospb.DETAIL_FIELD] = ('GnoiOsMgmt.activate missing '
                                             'version')
        return 0, json.dumps({ospb.ACTIVATE_ERROR_FIELD: activate_error})

      target_version = activate_request[ospb.VERSION_FIELD]
      if not self._version_string_valid(target_version):
        error_string = f'Activate called with invalid version: {target_version}'
        logger.error(error_string)
        return 0, json.dumps({
            ospb.ACTIVATE_ERROR_FIELD: {
                ospb.TYPE_FIELD: ospb.ACTIVATE_ERROR_UNSPECIFIED,
                ospb.DETAIL_FIELD: error_string
            }
        })

      # The request is for stack rollback.
      if target_version.startswith(STACK_ROLLBACK_VERSION_PREFIX):
        target_version = target_version[len(STACK_ROLLBACK_VERSION_PREFIX):]
        if self.stack_rollback(target_version):
          return 0, json.dumps({ospb.ACTIVATE_OK_FIELD: {}})
        logger.error('Rollback stack version %s does not exist', target_version)
        return 0, json.dumps({
            ospb.ACTIVATE_ERROR_FIELD: {
                ospb.TYPE_FIELD: ospb.ACTIVATE_ERROR_NON_EXISTENT_VERSION
            }
        })

      target_filepath = os.path.join(FE_TRANSFER_DIR, target_version)
      if not os.path.exists(target_filepath):
        logger.error('Target file %s does not exist', target_filepath)
        return 0, json.dumps({
            ospb.ACTIVATE_ERROR_FIELD: {
                ospb.TYPE_FIELD: ospb.ACTIVATE_ERROR_NON_EXISTENT_VERSION
            }
        })

      active_side = (not activate_request[ospb.STANDBY_SUPERVISOR_FIELD]
                     if ospb.STANDBY_SUPERVISOR_FIELD in activate_request else
                     True)
      for possible_output_location in [
          ACTIVE_SIDE_OUTPUT_LOCATION, INACTIVE_SIDE_OUTPUT_LOCATION
      ]:
        if os.path.exists(possible_output_location):
          logger.info('Discarding existing output file at destination %s',
                      possible_output_location)
          try:
            os.remove(possible_output_location)
          except Exception as e:
            logger.warning('Encountered exception trying to remove %s: %s',
                           possible_output_location, str(e))
      self.install_manager_thread = InstallManagerThread(
          target_filepath, active_side)
      self.install_manager_thread.start()

      return 0, json.dumps({ospb.ACTIVATE_OK_FIELD: {}})

    return 1, 'Failed to acquire lock (this should be impossible)'

  # 'as' = array of strings (reference dbus-python)
  # 'is' = integer, string
  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
  def verify(self, options):
    """Provides verification of the previous activation operation.

    Args:
      options: = string, json formatted VerifyRequest from os.proto

    Returns:
      int: 0 on success, 1 on failure
      string: formatted VerifyResponse from os.proto on success
              error string on failure
    """

    logger.info('Verify called')

    verify_result = {}
    version = get_version(True)
    if version:
      verify_result[ospb.VERSION_FIELD] = version
    if os.path.exists(ACTIVE_SIDE_OUTPUT_LOCATION):
      with open(ACTIVE_SIDE_OUTPUT_LOCATION) as file:
        verify_result[ospb.VERIFY_RESPONSE_FAIL_MESSAGE] = file.read()

    standby_response = {}
    version = get_version(False)
    if version:
      standby_response[ospb.VERSION_FIELD] = version
    if os.path.exists(INACTIVE_SIDE_OUTPUT_LOCATION):
      with open(INACTIVE_SIDE_OUTPUT_LOCATION) as file:
        standby_response[ospb.VERIFY_RESPONSE_FAIL_MESSAGE] = file.read()

    verify_result[ospb.VERIFY_RESPONSE_STANDBY] = ({
        ospb.VERIFY_STANDBY_RESPONSE: standby_response
    })

    return 0, json.dumps(verify_result)


def register():
  """Return the class name."""
  return GnoiOsMgmt, MOD_NAME
