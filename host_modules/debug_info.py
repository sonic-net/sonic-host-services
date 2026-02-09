"""Debug Artifact Collector.

This host service module implements the backend support for 
collecting host debug artifacts. Depending on the log level 
and board type input, the relevant log files, DB snapshots, 
counters, record files, and various command outputs are collected 
and aggregated under a specified artifact directory and the directory is 
compressed to a *.tar.gz in the host.

As part of the SONiC Debug artifact collection,below are the list of files.
core, log, db, counter files, routing.txt and version.txt
"""

from datetime import datetime
import json
import logging
import os
import shlex
import shutil
import subprocess

from host_modules import host_service
from utils.sonic_db_utils import SonicDbUtils
from swsscommon import swsscommon

MOD_NAME = "debug_info"
ARTIFACT_DIR = "/tmp/dump"
NONVOLATILE_PARTITION = "/var/log/"
NONVOLATILE_ARTIFACT_DIR = "/var/log/dump"
NONVOLATILE_STORAGE_REQUIRED = 5 * 10**8
NONVOLATILE_TMP_FLAG = "/tmp/nonvolatile_saved"
ARTIFACT_DIR_HOST = "host"
DB_ARTIFACT_DIR = ARTIFACT_DIR_HOST + "/db"
CORE_DIR = "core"
ARTIFACT_LEVEL_ALERT = "alert"
ARTIFACT_LEVEL_CRITICAL = "critical"
ARTIFACT_LEVEL_ALL = "all"
LOG_LEVEL_KEY = "level"
PERSISTENT_STORAGE_KEY = "use_persistent_storage"


DEBUG_INFO_FLAG = "debug_info"

logger = logging.getLogger(__name__)

class DebugArtifactCollector(host_service.HostModule):
  """DBus endpoint that collects debug artifacts."""

  def __init__(self, mod_name):
    self._hostname, self._board_type = DebugArtifactCollector.get_device_metadata()
    super(DebugArtifactCollector, self).__init__(mod_name)

  @staticmethod
  def _run_command(cmd: str, timeout: int = 20):
    """
    Modified to handle shell redirection logic in Python
    to avoid shell=True security risks.
    """
    # Check if the command has redirection (e.g., 'command > file')
    if " > " in cmd or " >> " in cmd:
      append = " >> " in cmd
      parts = cmd.split(" >> " if append else " > ")
      actual_cmd = shlex.split(parts[0])
      out_file = parts[1].strip()

      mode = "a" if append else "w"
      try:
        with open(out_file, mode) as f:
          proc = subprocess.Popen(
            actual_cmd,
            stdout=f, # Redirect stdout to file directly
            stderr=subprocess.PIPE,
            shell=False, # Semgrep safe
            text=True,
            close_fds=True)
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, "", stderr
      except Exception as e:
        return 1, "", str(e)

    # Standard command without redirection
    proc = subprocess.Popen(
        shlex.split(cmd),
        shell=False, # Semgrep safe
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
  def get_device_metadata() -> str:
    """
    Get hostname and board_type from CONFIG_DB
    using a single swsscommon call.
    """
    hostname = "switch"
    board_type = ""
    try:
      # Connect to CONFIG_DB
      db = swsscommon.SonicV2Connector()
      db.connect(db.CONFIG_DB)
      # Get device metadata
      metadata = db.get_all(db.CONFIG_DB, 'DEVICE_METADATA|localhost')
      db.close()
      if not metadata:
        logger.warning("DEVICE_METADATA|localhost empty or missing")
        return hostname, board_type
      hostname = metadata.get('hostname', hostname)
      board_type = metadata.get('platform', board_type)
    except Exception as e:
      logger.warning(f"Failed to read hostname/board_type from CONFIG_DB: {e}")
    return hostname, board_type
  
  @staticmethod
  def _collect_counter_artifacts(directory: str, prefix: str,
                                 board_type: str) -> None:
    COUNTER_CMDS = [
        "top -b -n 1 -w 500 > {}/top.txt",
        ('docker exec -i database '
        'redis-dump -H 127.0.0.1 -p 6379 -d 2 -y > {}/counter_db.json'),
    ]
    counter_artifact_dir = os.path.join(
        directory,
        datetime.now().strftime(prefix + "counter_%Y%m%d_%H%M%S"))
    os.makedirs(counter_artifact_dir, exist_ok=True)

    for cmd in COUNTER_CMDS:
      rc, _, err = DebugArtifactCollector._run_command(cmd.format(counter_artifact_dir), timeout=60)
      if rc != 0:
        # Continue the artifact collection in case of error.
        logger.warning(f"fail to execute command {cmd}, error:{err}")

  @staticmethod
  def _collect_teamdctl_data(artifact_dir_host):
    TEAMD_CTL_CMD = 'docker exec -i teamd teamdctl {} state dump'
    try:
      portchannels = SonicDbUtils.get_portchannels()
      if not portchannels:
        logger.warning("teamdctl: No PortChannels found, skipping teamdctl collection")
        return
      for trk in portchannels:
        teamdctl_cmd = shlex.split(TEAMD_CTL_CMD.format(trk))
        teamdctl_result = subprocess.run(
            teamdctl_cmd, shell=False, capture_output=True, text=True)
        if teamdctl_result.returncode == 0:
          filepath = os.path.join(artifact_dir_host, f'teamdctl_{trk}.txt')
          try:
            with open(filepath, 'w') as f:
              f.write(teamdctl_result.stdout)
              # If the filepath is invalid, then just return silently. If the
              # filepath is valid, the file will be created.
          except FileNotFoundError:
            logger.warning(
                f"teamdctl: Could not write file {filepath},invalid path. Skipping write."
              )
        else:
          logger.warning(
              f"Error running teamdctl for {trk}: {teamdctl_result.stderr}")
    except Exception as e:
      logger.warning(f"Failed to collect Portchannel data: {e}")

  @staticmethod
  def _save_persistent_storage(artifact_name: str) -> None:
    if os.path.isfile(NONVOLATILE_TMP_FLAG):
      logger.warning(
          f"already exists, skipping saving artifacts to "
          f"persistent storage {NONVOLATILE_TMP_FLAG}")
      return
    try:
      with open(NONVOLATILE_TMP_FLAG, "w+"):
        pass
    except OSError as e:
      logger.warning(f"error creating flag in tmp: {NONVOLATILE_TMP_FLAG}, Error: {e}")
      return

    host_artifact_name = ARTIFACT_DIR + "/" + artifact_name
    shutil.rmtree(NONVOLATILE_ARTIFACT_DIR, ignore_errors=True)
    try:
      artifact_size = os.path.getsize(host_artifact_name)
    except OSError:
      logger.warning(f"path {host_artifact_name} did not exist")
      return

    _, _, free = shutil.disk_usage(NONVOLATILE_PARTITION)
    if free < NONVOLATILE_STORAGE_REQUIRED + artifact_size:
      logger.warning(
          f"free space remaining on {NONVOLATILE_PARTITION}, "
          f"is less than {NONVOLATILE_STORAGE_REQUIRED + artifact_size}:{free}. "
          f"Not saving artifacts to persistent storage")
      return
    
    os.makedirs(NONVOLATILE_ARTIFACT_DIR, exist_ok=True)

    try:
      shutil.copy(host_artifact_name, os.path.join(NONVOLATILE_ARTIFACT_DIR, artifact_name))
    except Exception as e:
      logger.warning(f"fail to copy artifact to persistent storage: {e}")
 
  @staticmethod
  def _collect_host_files(artifact_dir_host):
    """
    Copy common host files/dirs into the artifact dir using Python APIs.
    This replaces shell 'cp -r' usage.
    """
    # 1) /var/log -> <artifact_dir_host>/var_log
    try:
      src = "/var/log"
      dst = os.path.join(artifact_dir_host, "var_log")
      if os.path.isdir(src):
        # copytree errors if dst exists; use copytree into a subdir name that doesn't exist yet
        if os.path.exists(dst):
          shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore_dangling_symlinks=True)
      else:
        logger.warning(f"_collect_host_files: {src} does not exist, skipping")
    except Exception as e:
      logger.warning(f"_collect_host_files: failed to copy /var/log: {e}")
 
    # 2) /var/core -> <artifact_dir_host>/core (if exists)
    try:
      src = "/var/core"
      dst = os.path.join(artifact_dir_host, "core")
      if os.path.isdir(src):
        if os.path.exists(dst):
          shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
      else:
        logger.warning(f"_collect_host_files: {src} does not exist, skipping")
    except Exception as e:
      logger.warning(f"_collect_host_files: failed to copy /var/core: {e}")
 
  @staticmethod
  def collect_artifacts(req: str, timestamp: str, board_type: str,
                        hostname: str):
    """Collect all artifacts for a given board type.

    Currently only host-level and DB artifacts are collected.
    Component-level (e.g., gnmi/orch) artifact collection is not supported

    This method can also be called by the CLI.

    Args:
      req: = string, a single JSON string that contains the log level, 
        and optional with persistent_storage flag to indicate if the artifacts should
        be stored in persistent storage, in addition to volatile storage
      timestamp: = string, a timestamp string that is used in the artifact name.
      board_type: = string, a string representation of the board type.
      hostname: = string, the hostname of the device, used to name the output
        directory.

    Returns:
      string: a return code and a return string to indicate the output artifact
      in the host.
    """
    COMMON_CMDS = [
        "show version > {}/version.txt",
        "ip -6 route > {}/routing.txt",
        "ip neigh >> {}/routing.txt",
        "ip route >> {}/routing.txt",
        "netstat -tplnaW | grep telemetry >> {}/routing.txt",
        "ip link >> {}/routing.txt",
    ]
    DB_CMDS = [
        ('docker exec -i database '
        'redis-dump -H 127.0.0.1 -p 6379 -d 0 -y > {}/appl_db.json'),
        ('docker exec -i database '
        'redis-dump -H 127.0.0.1 -p 6379 -d 1 -y > {}/asic_db.json'),
        ('docker exec -i database '
        'redis-dump -H 127.0.0.1 -p 6379 -d 4 -y > {}/config_db.json'),
        ('docker exec -i database '
        'redis-cli -n 1 hgetall VIDTORID > {}/vidtorid.txt'),
    ]
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

    # Prepare directories
    try:
      os.makedirs(artifact_dir_host, exist_ok=True)
    except Exception as e:
      logger.warning(f"collect_artifacts: failed to create artifact directory {artifact_dir_host}: {e}")
      return 1, str(e)
    
    # Collect counter artifacts at the beginning of the collection.
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      DebugArtifactCollector._collect_counter_artifacts(artifact_dir_host, "pre_",
                                           board_type)

    # Collect host files (replaces cp -r /var/log etc.)
    DebugArtifactCollector._collect_host_files(artifact_dir_host)
 
    # Collect routing/network text info and append outputs to routing.txt
    for cmd in COMMON_CMDS:
      rc, _, err = DebugArtifactCollector._run_command(cmd.format(artifact_dir_host))
      if rc != 0:
        # Continue the artifact collection in case of error.
        logger.warning(f"fail to execute command {cmd}, error:{err}")

    # Ensure core dir exists
    try:
      os.makedirs(os.path.join(artifact_dir_host, CORE_DIR), exist_ok=True)
    except Exception as e:
      logger.warning(f"collect_artifacts: failed to create core dir: {e}")

    DebugArtifactCollector._collect_teamdctl_data(artifact_dir_host)

    # DB commands collection (if requested by log_level)
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      os.makedirs(db_artifact_dir, exist_ok=True)
      for cmd in DB_CMDS:
        rc, _, err = DebugArtifactCollector._run_command(cmd.format(db_artifact_dir), timeout=60)
        if rc != 0:
          # Continue the artifact collection in case of error.
          logger.warning(f"fail to execute command {cmd}, error:{err}")

    # Collect counter artifacts at the end of the collection.
    if log_level == ARTIFACT_LEVEL_CRITICAL or log_level == ARTIFACT_LEVEL_ALL:
      DebugArtifactCollector._collect_counter_artifacts(artifact_dir_host, "post_",
                                           board_type)

    artifact_name = dir_name + ".tar.gz"
    host_artifact_name = ARTIFACT_DIR + "/" + artifact_name

    cmd = ("tar -C " + ARTIFACT_DIR + " -zcvf " + host_artifact_name + " " +
           dir_name)

    rc, _, err = DebugArtifactCollector._run_command(cmd, timeout=60)
    shutil.rmtree(os.path.join(ARTIFACT_DIR, dir_name), ignore_errors=True)
    if rc != 0:
      return rc, "fail to execute command '" + cmd + "': " + err

    if use_persistent_storage:
      DebugArtifactCollector._save_persistent_storage(artifact_name)
    
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

    if not self._board_type or self._hostname == "switch":
      self._hostname, self._board_type = DebugArtifactCollector.get_device_metadata()

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
  return DebugArtifactCollector, MOD_NAME
