"""GLOME DBus operations handler."""

import configparser
import json
import logging
import os
from pathlib import Path
import shutil
import stat

from host_modules import host_service

MOD_NAME = 'glome'
logger = logging.getLogger(__name__)

class Glome(host_service.HostModule):
  """DBus endpoint that executes GLOME operations on switch hosts."""

  _GLOME_PATH = '/host/glome/glome.conf'
  _GLOME_BACKUP_PATH = '/host/glome/glome_backup.conf'

  def _remove_config_file(self, file_path: str) -> None:
    try:
      os.remove(file_path)
    except FileNotFoundError:
      pass

  def _write_config_file(self, payload: dict[str, str]) -> None:
    file_path = Path(self._GLOME_PATH)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser(interpolation=None)
    config.add_section('service')
    config.set('service', 'key', str(payload['key']))
    config.set('service', 'key-version', str(payload['key_version']))
    config.set('service', 'url-prefix', str(payload['url_prefix']))
    with file_path.open('w') as f:
      config.write(f)
    os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is'
  )
  def push_config(self, request: str) -> tuple[int, str]:
    """Backs up and updates the GLOME configuration file.

    Creates a backup of the GLOME configuration file, then stores the request
    in the GLOME configuration file on the switch host.
    """
    try:
      # create a checkpoint first
      if os.path.exists(self._GLOME_PATH):
        # copy() copies the file data and the file’s permission mode.
        shutil.copy(src=self._GLOME_PATH, dst=self._GLOME_BACKUP_PATH)
      else:
        self._remove_config_file(self._GLOME_BACKUP_PATH)
      # process the request
      payload = json.loads(request)
      if payload['enabled']:
        self._write_config_file(payload)
      else:
        self._remove_config_file(self._GLOME_PATH)
    except PermissionError as e:
      logger.error('PermissionError: %s\nrequest: %s', request, e)
      return 1, f'A PermissionError error occurred: {e}'
    except OSError as e:
      logger.error('OSError: %s\nrequest: %s', request, e)
      return 2, f'An OSError error occurred: {e}'
    except json.decoder.JSONDecodeError as e:
      logger.error('JSONDecodeError: %s\nrequest: %s', request, e)
      return 3, f'A JSONDecodeError error occurred: {e}'
    except KeyError as e:
      logger.error('KeyError: %s\nrequest: %s', request, e)
      return 4, f'A KeyError error occurred: {e}'
    return 0, ''

  @host_service.method(
      host_service.bus_name(MOD_NAME), in_signature='', out_signature='is'
  )
  def restore_checkpoint(self) -> tuple[int, str]:
    """Restores the GLOME configuration file to the backup file."""
    try:
      if os.path.exists(self._GLOME_BACKUP_PATH):
        # copy() copies the file data and the file’s permission mode.
        shutil.copy(src=self._GLOME_BACKUP_PATH, dst=self._GLOME_PATH)
      else:
        self._remove_config_file(self._GLOME_PATH)
    except PermissionError as e:
      logger.error('PermissionError: %s', e)
      return 1, f'A PermissionError error occurred: {e}'
    except OSError as e:
      logger.error('OSError: %s', e)
      return 2, f'An OSError error occurred: {e}'
    return 0, ''


def register():
  """Return class name."""
  return Glome, MOD_NAME

