import csv
import logging
import os
import yaml

logger = logging.getLogger(__name__)

debian_name = 'Debian GNU/Linux'
switch_linux_name = 'Switch Linux'

switch_linux_version_path = '/lib/google/gpins_version.yml'
debian_version_path = '/etc/os-release'
sonic_version_paths = [
  '/etc/sonic/sonic_version.yml',
  '/mnt/region_config/container_files/etc/sonic/sonic_version.yml'
]

# Use a global to cache platform info, which does not change at runtime
_platform = None

def _read_yaml_file(path):
  with open(path, 'r') as f:
    try:
        data = yaml.safe_load(f)
        return data
    except yaml.YAMLError as e:
        logger.error(f"Error parsing {path}: {e}")
  return {}

def _read_os_version_file(path):
  with open(path, 'r') as f:
    reader = csv.reader(f, delimiter='=', quotechar='"')
    data = dict(reader)
    return data

def _read_platform_info():
  global _platform
  if _platform is not None:
    return _platform

  platform = {
    'os': None,
    'asic_type': None,
    'is_switch_linux': False,
    'is_debian': False,
    'is_sonic': False,
  }

  if os.path.isfile(switch_linux_version_path):
    platform['os'] = switch_linux_name
    switch_linux_data = _read_yaml_file(switch_linux_version_path)
    platform['asic_type'] = switch_linux_data.get('asic_type')
    platform['is_switch_linux'] = True
  elif os.path.isfile(debian_version_path):
    debian_data = _read_os_version_file(debian_version_path)
    os_name = debian_data.get('NAME')
    platform['os'] = os_name
    platform['is_debian'] = debian_name in os_name
  else:
    logger.debug('OS version file not found')

  for path in sonic_version_paths:
    if os.path.isfile(path):
      sonic_version_data = _read_yaml_file(path)
      platform['asic_type'] = sonic_version_data.get('asic_type')
      platform['is_sonic'] = True
      break
  if platform.get('asic_type') is None:
      logger.debug('SONiC version file not found')

  logger.info(f'Platform info: {platform}')
  # Note: the value of platform is deterministic and simple assignment is threadsafe
  _platform = platform

# Populate the global _platform variable when the module is loaded
_read_platform_info()

def get_platform_info():
  'Return information about the platform, including OS name and switch ASIC type'
  if _platform is None:
    _read_platform_info()
  return _platform

def is_sonic_debian():
  'Return True if Debian is detected as the OS and sonic_version.yml is present'
  return get_platform_info().get('is_debian') and get_platform_info().get('is_sonic')

def is_sonic_switch_linux():
  'Return True if Switch Linux is detected as the OS and sonic_version.yml is present'
  return get_platform_info().get('is_switch_linux') and get_platform_info().get('is_sonic')

def get_platform_asic():
  'Returns the switch ASIC type as a string'
  return get_platform_info().get('asic_type')

if __name__ == "__main__":
  print(get_platform_info())
