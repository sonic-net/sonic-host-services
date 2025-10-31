"""Debug Info.

This host service module implements the backend support for collecting debug
artifacts.
"""

from datetime import datetime
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from host_modules import host_service
import redis
from swsscommon import swsscommon
from utils import platform_info

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
SDK_ARTIFACT_DIR = ARTIFACT_DIR_HOST + get_sdk_artifact_dir()
COMPONENT_ALL = "all"
ARTIFACT_LEVEL_ALERT = "alert"
ARTIFACT_LEVEL_CRITICAL = "critical"
ARTIFACT_LEVEL_ALL = "all"
COMPONENT_KEY = "component"
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

COMPONENTS = [
    "orch",
    "gnmi",
]

TOTAL_COMPONENT_TIMEOUT_SECS = 120
MIN_SELECT_TIME_SECS = 20
SELECT_TIMEOUT_MSECS = 1000

logger = logging.getLogger(__name__)

class DebugInfo(host_service.HostModule):
  """DBus endpoint that collects debug artifacts."""

  def __init__(self, mod_name):
    self._lock = threading.Lock()
    self._ongoing_thread = ""
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
  def _sanitize_filename(filename: str) -> str:
    r"""Sanitize the filenames to remove or replace illegal character.

    Below is a table of characters and their replacements:
        Char    Replacement
        "'"     ""
        ";"     ""
        " "     "_"
        "/"     "+"
        "|"     "\|"

    Args:
      filename: = string, a file name to sanitize.

    Returns:
      string: The sanitized filename.
    """
    return filename.replace("'", "").replace(";", "").replace(" ", "_").replace(
        "/", "+").replace("|", r"\|")

  @staticmethod
  def _send_component_request(component, dir_name, log_level, producer):
    components = {}
    for component_id in COMPONENTS:
      if component == COMPONENT_ALL or component == component_id:
        # make the artifact dir in the host
        host_artifact_dir = os.path.join(ARTIFACT_DIR, dir_name, component_id)
        os.makedirs(host_artifact_dir, exist_ok=True)
        
        # send container artifact dir in component request
        container_artifact_dir = os.path.join(ARTIFACT_DIR_CONTAINER, dir_name,
                                              component_id)
        fvs = swsscommon.FieldValuePairs([(LOG_LEVEL_KEY, log_level)])
        producer.send(component_id, container_artifact_dir, fvs)
        components[component_id] = container_artifact_dir
    return components

  @staticmethod
  def _get_component_response(start_time, components, sel, consumer):
    completed_components = set()
    time_bound = max(time.time() + MIN_SELECT_TIME_SECS,
                     start_time + TOTAL_COMPONENT_TIMEOUT_SECS)

    while time.time() < time_bound:
      (state, _) = sel.select(SELECT_TIMEOUT_MSECS)

      if state == swsscommon.Select.TIMEOUT:
        continue

      if state != swsscommon.Select.OBJECT:
        logger.warning("select did not return swsscommon.Select.OBJECT")
        continue
      
      (op, data, fvs) = consumer.pop()
    
      if op in completed_components:
        logger.warning("Duplicate component response: %s", op)
      elif DebugInfo._validate_component_response(components, op, data, fvs):
        components.pop(op)
        completed_components.add(op)

      if not components:  # finish collecting all components
        break
    for component in components:
      logger.warning("Failed to collect component: %s", component)

  @staticmethod
  def _validate_component_response(components, op, data, fvs) -> bool:
    """Returns True if component response is valid, False otherwise."""
    if op not in components:
      logger.warning("Invalid component: %s", op)
      return False

    if components[op] != data:
      logger.warning(
          "Response directory %s does not match with request directory %s",
          data, components[op])
      return False

    pairs = dict(fvs)

    if "status" not in pairs:
      logger.warning("Response missing status field")
      return False
    if "err_str" not in pairs:
      logger.warning("Response missing err_str field")
      return False
    if pairs["status"] != "success":
      logger.warning("%s component failed: %s", op, pairs["err_str"])

    return True

  @staticmethod
  def _collect_teamdctl_data(artifact_dir_host):
    try:
      redis_result = subprocess.run(
          REDIS_LIST_PC_CMD, shell=True, capture_output=True, text=True, check=True)
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

    This method can also be called by the CLI.

    Args:
      req: = string, a single JSON string that contains the component name, log
        level, and a persistent_storage flag to indicate if the artifacst should
        be stored in persistent storage, in addition to volatile storage
      timestamp: = string, a timestamp string that is used in the artifact name.
      board_type: = string, a string representation of the board type.
      hostname: = string, the hostname of the device, used to name the output
        directory.

    Returns:
      string: a return code and a return string to indicate the output artifact
      in the host.
    """
    app_db = swsscommon.DBConnector("APPL_DB", 0, True)
    producer = swsscommon.NotificationProducer(app_db, "DEBUG_DATA_REQ_CHANNEL")
    consumer = swsscommon.NotificationConsumer(app_db,
                                               "DEBUG_DATA_RESP_CHANNEL")
    sel = swsscommon.Select()
    sel.addSelectable(consumer)
    
    try:
      request = json.loads(req)
    except json.JSONDecodeError:
      return 1, "invalid input: " + req
    component = request.get(COMPONENT_KEY, COMPONENT_ALL)
    log_level = request.get(LOG_LEVEL_KEY, ARTIFACT_LEVEL_ALERT)
    use_persistent_storage = request.get(
        PERSISTENT_STORAGE_KEY) if PERSISTENT_STORAGE_KEY in request else False
    
    dir_name = hostname + "_" + timestamp
    artifact_dir_host = os.path.join(ARTIFACT_DIR, dir_name, ARTIFACT_DIR_HOST)
    db_artifact_dir = os.path.join(ARTIFACT_DIR, dir_name, DB_ARTIFACT_DIR)

    os.makedirs(artifact_dir_host, exist_ok=True)
    
    try:
        components = DebugInfo._send_component_request(component, dir_name,
                                                   log_level, producer)
    except Exception as e:
        logger.error("Error in send_component_request: %s", str(e))

    start_time = time.time()

    # Collect counter artifacts at the beginning of the collection.
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      DebugInfo._collect_counter_artifacts(artifact_dir_host, "pre_",
                                           board_type)

    for cmd in CMN_CMDS:
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

    DebugInfo._get_component_response(start_time, components, sel, consumer)

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

  def _collect_artifacts_thread(self, req: str, timestamp: str, board_type: str,
                                hostname: str):
    self.collect_artifacts(req, timestamp, board_type, hostname)
    with self._lock:
      self._ongoing_thread = ""

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def collect(self, options):
    """
    # Always succeed, ignore input
    dummy_artifact_path = "/tmp/dummy_healthz_artifact.tar.gz"

    # Optional: create the dummy artifact file
    try:
        with open(dummy_artifact_path, "w") as f:
            f.write("Dummy Healthz artifact content")
    except Exception as e:
        return 1, "Failed to create dummy artifact: " + str(e)

    with self._lock:
        self._ongoing_thread = dummy_artifact_path

    return 0, dummy_artifact_path
    """
    
    try:
      json.loads(options)
    except json.JSONDecodeError:
      return 1, "invalid input: " + options

    with self._lock:
      if self._ongoing_thread:
        return 1, "Previous artifact collection is ongoing"
    
    if not self._board_type:
      self._board_type = self.get_board_type()
    if self._hostname == "switch":
      self._hostname = self.get_hostname()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    artifact_name = self._hostname + "_" + timestamp + ".tar.gz"
    container_artifact_name = ARTIFACT_DIR + "/" + artifact_name

    with self._lock:
      self._ongoing_thread = container_artifact_name

    # Issue artifact collection in a new thread.
    try:
      t = threading.Thread(
          target=self._collect_artifacts_thread,
          args=(options, timestamp, self._board_type, self._hostname))
      t.start()
    except RuntimeError as error:
      with self._lock:
        self._ongoing_thread = ""
      return 1, "Failed to start artifact collection thread with error: " + str(
          error)
    return 0, container_artifact_name
    

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def check(self, options):
    with self._lock:
      if self._ongoing_thread == options:
        return 1, "Artifact not ready"
    return 0, "Artifact ready"

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature="as", out_signature="is")
  def ack(self, options):
    # The artifact name in container has a different prefix. Convert it to the
    # host.
    artifact = ARTIFACT_DIR + options.removeprefix(ARTIFACT_DIR)
    try:
      os.remove(artifact)
      return 0, ""
    except OSError as error:
      return 1, "Failed to delete artifact file with error: " + str(error)

def register():
  """Return class name."""
  return DebugInfo, MOD_NAME
