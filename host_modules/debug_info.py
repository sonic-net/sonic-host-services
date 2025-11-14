"""Debug Info.

This host service module implements the backend support for 
collecting host debug artifacts.Depending on the log level 
and board type input,the relevant log files, DB snapshots, 
counters, record files,and various command outputs are collected 
and aggregated under a specified artifact directory and the directory is 
compressed to a *.tar.gz in the host.

As part of the SONiC supported common debug commands,below are the list of files.
core,log,db,counter files,routing.txt and version.txt
"""

from datetime import datetime
import json
import logging
import os
import shutil
import subprocess
import time

from host_modules import host_service
# Import SONiC debug commands for SONiC platform.
from utils.sonic_debug_cmds import *

MOD_NAME = "debug_info"
ARTIFACT_DIR = "/tmp/dump"
NONVOLATILE_PARTITION = "/var/log/"
NONVOLATILE_ARTIFACT_DIR = "/var/log/dump"
NONVOLATILE_STORAGE_REQUIRED = 5 * 10**8
NONVOLATILE_TMP_FLAG = "/tmp/nonvolatile_saved"
ARTIFACT_DIR_CONTAINER = "/var/dump"
ARTIFACT_DIR_HOST = "host"
CORE_DIR = "core"
DB_ARTIFACT_DIR = ARTIFACT_DIR_HOST + "/db"
ARTIFACT_LEVEL_ALERT = "alert"
ARTIFACT_LEVEL_CRITICAL = "critical"
ARTIFACT_LEVEL_ALL = "all"
LOG_LEVEL_KEY = "level"
PERSISTENT_STORAGE_KEY = "use_persistent_storage"

STATE_DB_SEPARATOR = "|"
DEBUG_INFO_FLAG = "debug_info"

log_dir = "/var/log"
os.makedirs(log_dir, exist_ok=True)

log_file = os.path.join(log_dir, "debug_info.log")
logging.basicConfig(
    filename=log_file,
    filemode='a',  # append mode
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)

logger = logging.getLogger(__name__)

class DebugInfo(host_service.HostModule):
  """DBus endpoint that collects debug artifacts."""

  def __init__(self, mod_name):
    self._board_type = DebugInfo.get_board_type()
    self._hostname = DebugInfo.get_hostname()
    super(DebugInfo, self).__init__(mod_name)

  @staticmethod
  def _run_command(cmd: str, timeout: int = 20):
    proc = subprocess.Popen(
        cmd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True)
    try:
      stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      proc.kill()
      return 1, "command timeout", "command timeout"
    return proc.returncode, stdout, stderr

  @staticmethod
  def get_board_type() -> str:
    rc, stdout, err = DebugInfo._run_command(BOARD_TYPE_CMD)
    board_type = ""
    if rc != 0:
      logger.warning("fail to execute command '%s': %s", BOARD_TYPE_CMD, err)
    else:
      board_type = stdout.strip()
    return board_type

  @staticmethod
  def get_hostname() -> str:
    cmd = "hostname"
    rc, stdout, err = DebugInfo._run_command(cmd)
    hostname = "switch"
    if rc != 0:
      logger.warning("fail to execute command '%s': %s", cmd, err)
    else:
      hostname = stdout.strip()
    return hostname

  @staticmethod
  def _collect_counter_artifacts(directory: str, prefix: str,
                                 board_type: str) -> None:
    counter_artifact_dir = os.path.join(
        directory,
        datetime.now().strftime(prefix + "counter_%Y%m%d_%H%M%S"))
    os.makedirs(counter_artifact_dir, exist_ok=True)

    for cmd in COUNTER_CMDS:
      rc, _, err = DebugInfo._run_command(cmd.format(counter_artifact_dir), timeout=60)
      if rc != 0:
        # Continue the artifact collection in case of error.
        logger.warning("fail to execute command '%s': %s", cmd, err)

  @staticmethod
  def _collect_teamdctl_data(artifact_dir_host):
    try:
      redis_result = subprocess.run(
        REDIS_LIST_PORTCHANNEL_CMD, shell=True, capture_output=True, text=True, check=True)
      trunks = redis_result.stdout.strip().split('\n')
      for trunk in trunks:
        try:
          trk = trunk.split('|')[1]
        except IndexError:
          # No trunk is found in the DB or the trunk table format is incorrect.
          continue
        teamdctl_cmd = TEAMD_CTL_CMD.format(trk)
        teamdctl_result = subprocess.run(
            teamdctl_cmd, shell=True, capture_output=True, text=True)
        if teamdctl_result.returncode == 0:
          filepath = os.path.join(artifact_dir_host, f'teamdctl_{trk}.txt')
          try:
            with open(filepath, 'w') as f:
              f.write(teamdctl_result.stdout)
          # If the filepath is invalid, then just return silently. If the
          # filepath is valid, the file will be created.
          except FileNotFoundError:
            return
        else:
          logger.warning(
              f"Error running teamdctl for {trk}: {teamdctl_result.stderr}")
    except subprocess.CalledProcessError as e:
      logger.warning(f"Error running Redis command: {e}")

  @staticmethod
  def _save_persistent_storage(artifact_name: str) -> None:
    if os.path.isfile(NONVOLATILE_TMP_FLAG):
      logger.warning(
          "%s already exists, skipping saving artifacts to "
          "persistent storage", NONVOLATILE_TMP_FLAG)
      return
    try:
      with open(NONVOLATILE_TMP_FLAG, "w+"):
        pass
    except OSError as e:
      logger.warning("error creating flag in tmp: %s. Error: %s",
                     NONVOLATILE_TMP_FLAG, str(e))
      return

    host_artifact_name = ARTIFACT_DIR + "/" + artifact_name
    shutil.rmtree(NONVOLATILE_ARTIFACT_DIR, ignore_errors=True)
    try:
      artifact_size = os.path.getsize(host_artifact_name)
    except OSError:
      logger.warning("path %s did not exist", host_artifact_name)
      return

    _, _, free = shutil.disk_usage(NONVOLATILE_PARTITION)
    if free < NONVOLATILE_STORAGE_REQUIRED + artifact_size:
      logger.warning(
          "free space remaining on %s is less than %d: %d. Not saving "
          "artifacts to persistent storage", NONVOLATILE_PARTITION,
          NONVOLATILE_STORAGE_REQUIRED + artifact_size, free)
      return
    
    os.makedirs(NONVOLATILE_ARTIFACT_DIR, exist_ok=True)

    cmd = (
        f"cp {host_artifact_name} {NONVOLATILE_ARTIFACT_DIR}/{artifact_name}")
    
    rc, _, err = DebugInfo._run_command(cmd)
    if rc != 0:
      # Report success overall if saving to persistent storage fails, saving
      # to persistent storage is best-effort.
      logger.warning("fail to execute command '%s': %s", cmd, err)

  @staticmethod
  def collect_artifacts(req: str, timestamp: str, board_type: str,
                        hostname: str):
    """Collect all artifacts for a given board type.

    Currently only host-level and DB artifcats are collected.
    Component-level (e.g., gnmi/orch) artifact collection is not supported

    This method can also be called by the CLI.

    Args:
      req: = string, a single JSON string that contains the log level, 
        and optional with persistent_storage flag to indicate if the artifacst should
        be stored in persistent storage, in addition to volatile storage
      timestamp: = string, a timestamp string that is used in the artifact name.
      board_type: = string, a string representation of the board type.
      hostname: = string, the hostname of the device, used to name the output
        directory.

    Returns:
      string: a return code and a return string to indicate the output artifact
      in the host.
    """
    try:
      request = json.loads(req)
    except json.JSONDecodeError:
      return 1, "invalid input: " + req
    log_level = request.get(LOG_LEVEL_KEY, ARTIFACT_LEVEL_ALERT)
    use_persistent_storage = request.get(
        PERSISTENT_STORAGE_KEY) if PERSISTENT_STORAGE_KEY in request else False
    
    dir_name = hostname + "_" + timestamp
    artifact_dir_host = os.path.join(ARTIFACT_DIR, dir_name, ARTIFACT_DIR_HOST)
    db_artifact_dir = os.path.join(ARTIFACT_DIR, dir_name, DB_ARTIFACT_DIR)

    os.makedirs(artifact_dir_host, exist_ok=True)
    
    # Collect counter artifacts at the beginning of the collection.
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      DebugInfo._collect_counter_artifacts(artifact_dir_host, "pre_",
                                           board_type)

    for cmd in COMMON_CMDS:
      rc, _, err = DebugInfo._run_command(cmd.format(artifact_dir_host))
      if rc != 0:
        # Continue the artifact collection in case of error.
        logger.warning("fail to execute command '%s': %s", cmd, err)

    # create host/core dir if it does not exist
    os.makedirs(artifact_dir_host + "/" + CORE_DIR, exist_ok=True)

    DebugInfo._collect_teamdctl_data(artifact_dir_host)

    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      os.makedirs(db_artifact_dir, exist_ok=True)
      for cmd in DB_CMDS:
        rc, _, err = DebugInfo._run_command(cmd.format(db_artifact_dir), timeout=60)
        if rc != 0:
          # Continue the artifact collection in case of error.
          logger.warning("fail to execute command '%s': %s", cmd, err)

    # Collect counter artifacts at the end of the collection.
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      DebugInfo._collect_counter_artifacts(artifact_dir_host, "post_",
                                           board_type)

    artifact_name = dir_name + ".tar.gz"
    host_artifact_name = ARTIFACT_DIR + "/" + artifact_name

    cmd = ("tar -C " + ARTIFACT_DIR + " -zcvf " + host_artifact_name + " " +
           dir_name)

    rc, _, err = DebugInfo._run_command(cmd, timeout=60)
    shutil.rmtree(os.path.join(ARTIFACT_DIR, dir_name), ignore_errors=True)
    if rc != 0:
      return rc, "fail to execute command '" + cmd + "': " + err

    if use_persistent_storage:
      DebugInfo._save_persistent_storage(artifact_name)
    
    return 0, host_artifact_name

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def collect(self, options):
    """DBus entrypoint to collect debug artifacts from host"""
    # Converts single string input into a one-element list.
    if isinstance(options, str):
        options = [options]
    try:
      json.loads(options[0])
    except json.JSONDecodeError:
      return 1, "invalid input: " + options[0]

    if not self._board_type:
      self._board_type = self.get_board_type()
    if self._hostname == "switch":
      self._hostname = self.get_hostname()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    try:
      rc, artifact_path = self.collect_artifacts(options[0], timestamp, self._board_type, self._hostname)
    except Exception as error:
      return 1, "Artifact collection failed: " + str(
          error)
    if rc != 0:
      return rc, artifact_path
    return 0, artifact_path

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def check(self, options):
    """Always ready because artifact collection is synchronous."""
    return 0, "Artifact ready"

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def ack(self, options):
    # The artifact name in container has a different prefix. Convert it to the
    # host.
    if isinstance(options, str):
        options = [options]
    artifact = ARTIFACT_DIR + options[0].removeprefix(ARTIFACT_DIR)
    try:
      os.remove(artifact)
    except FileNotFoundError:
      return 1, "Artifact file not found: " + str(artifact)
    except PermissionError:
      return 1, "Artifact file permission denied: " + str(artifact)
    except OSError as error:
      return 1, "Failed to delete artifact file with error: " + str(error)
    return 0, ""

def register():
  """Return class name."""
  return DebugInfo, MOD_NAME
