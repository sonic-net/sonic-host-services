import builtins
import io
import os
import sys
import unittest
from unittest import mock

import redis

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
scripts_path = os.path.join(sonic_host_service_path, 'scripts')
sys.path.insert(0, scripts_path)

import pins_platform_init as ppi


class TestPinsPlatformInit(unittest.TestCase):

  def test_init(self):
    with mock.patch('redis.Redis', autospec=True) as mock_redis:
      ppi.PinsPlatformInit()
      mock_redis.assert_call_count = 3
      mock_redis.assert_has_calls(
          calls=[
              mock.call(
                  host=ppi.REDIS_HOST,
                  port=ppi.REDIS_PORT_NUMBER,
                  db=ppi.REDIS_CONFIG_DB_NUMBER,
              ),
              mock.call(
                  host=ppi.REDIS_HOST,
                  port=ppi.REDIS_PORT_NUMBER,
                  db=ppi.REDIS_STATE_DB_NUMBER,
              ),
              mock.call(
                  host=ppi.REDIS_HOST,
                  port=ppi.REDIS_PORT_NUMBER,
                  db=ppi.REDIS_APPL_STATE_DB_NUMBER,
              ),
          ]
      )

  @mock.patch(
      'pins_platform_init.PinsPlatformInit.__init__',
      side_effect=redis.RedisError,
  )
  def test_assert_redis_error(self, mock_init):
    with self.assertRaises(redis.RedisError):
      ppi.PinsPlatformInit()
    mock_init.assert_called_once()

  def test_primary_version_dict(self):
    platform = ppi.PinsPlatformInit()
    platform.primary_version_dict = {
        'build_version': (
            'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3'
        ),
        'debian_version': '11.11',
        'kernel_version': '5.10.0-30-2-amd64',
        'asic_type': 'cisco-8000',
        'asic_subtype': 'cisco-8000',
        'commit_id': '44c7e0b3',
        'branch': 'HEAD',
        'release': '202305',
        'build_date': 'Sat May 17 00:08:15 UTC 2025',
        'build_number': (
            'pins/sonic-buildimage/gcp_ubuntu/presubmit_cisco/3138'
        ),
# copybara:strip_begin(internal kokoro builder)
        'built_by': 'kbuilder@kokoro-gcp-ubuntu-prod-484706552',
# copybara:strip_end
        'libswsscommon': '1.0.0',
        'sonic_utilities': '1.2.0',
        'sonic_os_version': '11',
    }
    self.assertEqual(
        platform.primary_version_dict['build_version'],
        'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3',
    )

  def test_available_network_stacks_with_same_current_and_next(self):
    platform = ppi.PinsPlatformInit()
    platform.run_command = mock.MagicMock()
    platform.run_command.return_value = """
    Current: SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3
    Next: SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3
    Available:
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3139-44c7e0b3
    """
    expected_network_stacks = [
        'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3',
        'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3139-44c7e0b3',
    ]
    self.assertCountEqual(
        platform.get_network_stacks(ppi.AVAILABLE), expected_network_stacks
    )
    self.assertCountEqual(
        platform.get_network_stacks(ppi.CURRENT),
        [
            'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3'
        ],
    )
    self.assertCountEqual(
        platform.get_network_stacks(ppi.NEXT),
        [
            'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3'
        ],
    )

  def test_available_network_stacks_with_different_current_and_next(self):
    platform = ppi.PinsPlatformInit()
    platform.run_command = mock.MagicMock()
    platform.run_command.return_value = """
    Current: SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3
    Available:
    SONiC-OS-HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3
    """
    expected_network_stacks = [
        'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3',
    ]
    self.assertCountEqual(
        platform.get_network_stacks(ppi.AVAILABLE), expected_network_stacks
    )
    self.assertCountEqual(
        platform.get_network_stacks(ppi.CURRENT),
        [
            'HEAD.pins_sonic-buildimage_gcp_ubuntu_presubmit_cisco_3138-44c7e0b3'
        ],
    )

  def test_write_chassis_info_paths_arista(self):
    config_db_platforms = [
        b'x86_64-arista_7060xe7_64ps-r0',
        b'x86_64-arista_7060xe7_64p-r1',
    ]
    for platform in config_db_platforms:
      with mock.patch.object(
          redis.Redis, 'hget', return_value=platform
      ) as mock_hget:
        platform_init = ppi.PinsPlatformInit()
        platform_init.primary_version_dict = {
            'build_version': (
                'pins_daily_20260507_19_artemis_RC00'
            ),
        }
        platform_init.state_db = mock.MagicMock()
        platform_init.write_chassis_info_paths()
        mock_hget.assert_called_once_with(ppi.DEVICE_METADATA, ppi.PLATFORM)
        if hasattr(platform_init, 'platform_name'):
          self.assertEqual('banff', platform_init.platform_name)
          platform_init.state_db.hset.assert_has_calls([
              mock.call(
                  ppi.CHASSIS_INFO, ppi.PLATFORM, platform_init.platform_name
              ),
              mock.call(
                  ppi.CHASSIS_INFO,
                  ppi.FW_VERSION,
                  'pins_daily_20260507_19_artemis_RC00',
              ),
          ])

  def test_populate_version_dictionary(self):
    file_name = os.path.join(test_path, 'test_data', 'test_file.txt')
    expected_dictionary = {
        'key1': 'value1',
        'key2': 'value2',
        'key3': 'value3',
    }
    with mock.patch.object(
        builtins, 'open', return_value=mock.MagicMock()
    ) as mock_open:
      mock_open.return_value.__enter__.return_value = [
          'key1: value1',
          'key2: value2',
          'key3: value3',
          'key4  value4',
      ]
      dictionary = ppi.PinsPlatformInit().populate_version_dictionary(file_name)
      self.assertEqual(dictionary, expected_dictionary)

  def test_populate_version_dictionary_with_separator(self):
    file_name = os.path.join(test_path, 'test_data', 'test_file.txt')
    expected_dictionary = {
        'key1': 'value1',
        'key2': 'value2',
        'key3': 'value3',
    }
    with mock.patch.object(
        builtins, 'open', return_value=mock.MagicMock()
    ) as mock_open:
      mock_open.return_value.__enter__.return_value = [
          'key1=value1',
          'key2=value2',
          'key3=value3',
      ]
      dictionary = ppi.PinsPlatformInit().populate_version_dictionary(
          file_name, '='
      )
      self.assertEqual(dictionary, expected_dictionary)

  @mock.patch(
      'pins_platform_init.PinsPlatformInit.populate_version_dictionary',
      side_effect=ValueError,
  )
  def test_populate_version_dictionary_assert_value_error(self, mock_error):
    file_name = os.path.join(test_path, 'test_data', 'test_file.txt')
    with self.assertRaises(ValueError):
      ppi.PinsPlatformInit().populate_version_dictionary(file_name)
    mock_error.assert_called_once()

  @mock.patch(
      'pins_platform_init.PinsPlatformInit.populate_version_dictionary',
      side_effect=OSError,
  )
  def test_populate_version_dictionary_assert_os_error(self, mock_error):
    file_name = os.path.join(test_path, 'test_data', 'test_file.txt')
    with self.assertRaises(OSError):
      ppi.PinsPlatformInit().populate_version_dictionary(file_name)
    mock_error.assert_called_once()

  def test_populate_version_dictionaries(self):
    platform = ppi.PinsPlatformInit()
    platform.state_db = mock.MagicMock()
    platform.populate_version_dictionary = mock.MagicMock()
    platform.populate_version_dictionary.side_effect = [
        {},
        # {'image1': {'build_version': 'version1'}},
        {'build_version': 'version2'},
        {'build_version': 'version3'},
    ]
    platform.get_network_stacks = mock.MagicMock()
    platform.get_network_stacks.side_effect = [
        'image1',
        ['image1', 'image2', 'image3'],
    ]
    platform.run_command = mock.MagicMock()
    with mock.patch(
        'pins_platform_init.os.path.exists', return_value=True, autospec=True
    ):
      with mock.patch(
          'pins_platform_init.os.makedirs', autospec=True
      ) as mock_makedirs:
        with mock.patch(
            'pins_platform_init.shutil.rmtree', autospec=True
        ) as mock_rmtree:
          platform.populate_version_dictionaries()
          self.assertEqual(mock_makedirs.call_count, 2)
          self.assertEqual(
              platform.alternate_versions_dict,
              {
                  'image2': {'build_version': 'version2'},
                  'image3': {'build_version': 'version3'},
              },
          )
          platform.run_command.assert_has_calls([
              mock.call([
                  'unsquashfs',
                  '-n',
                  '-q',
                  '-f',
                  '-d',
                  ppi.SQUASHFS_EXTRACT_PATH,
                  f'/host/image-image2/fs.squashfs',
                  f'{ppi.VERSION_FILE}',
              ]),
              mock.call([
                  'unsquashfs',
                  '-n',
                  '-q',
                  '-f',
                  '-d',
                  ppi.SQUASHFS_EXTRACT_PATH,
                  f'/host/image-image3/fs.squashfs',
                  f'{ppi.VERSION_FILE}',
              ]),
          ])
          self.assertEqual(mock_rmtree.call_count, 2)

  def test_write_sw_bootloader_paths(self):
    platform = ppi.PinsPlatformInit()
    platform.run_command = mock.MagicMock()
    platform.run_command.return_value = '1.1.1'
    platform.state_db = mock.MagicMock()
    platform.populate_version_dictionary = mock.MagicMock()
    platform.populate_version_dictionary.return_value = {
        'onie_version': '2020.11br.CISCO-dirty',
        'onie_kernel_version': '4.9.581',
    }
    platform.write_sw_bootloader_paths()
    platform.state_db.hset.assert_any_call(
        ppi.SW_COMP_INFO_BOOTLOADER, ppi.SOFTWARE_VERSION, '1.1.1'
    )

  def test_write_sw_network_stack_paths(self):
    platform = ppi.PinsPlatformInit()
    platform.primary_version_dict = {
        'build_version': 'pins_primary_version',
    }
    platform.alternate_versions_dict = {
        'image1': {'build_version': 'pins_alternate_version'}
    }
    platform.state_db = mock.MagicMock()
    platform.write_sw_network_stack_paths()
    platform.state_db.hset.assert_any_call(
        ppi.SW_COMP_INFO_STACK0, ppi.OPER_STATUS, ppi.ACTIVE
    )
    platform.state_db.hset.assert_any_call(
        ppi.SW_COMP_INFO_STACK0, ppi.SOFTWARE_VERSION, 'pins_primary_version'
    )
    platform.state_db.hset.assert_any_call(
        'SW_COMP_INFO|network_stack1', ppi.OPER_STATUS, ppi.INACTIVE
    )
    platform.state_db.hset.assert_any_call(
        'SW_COMP_INFO|network_stack1',
        ppi.SOFTWARE_VERSION,
        'pins_alternate_version',
    )

  def test_write_sw_network_stack_paths_no_build_version(self):
    platform = ppi.PinsPlatformInit()
    platform.primary_version_dict = {
        'not_build_version': 'some_verson',
    }
    platform.alternate_versions_dict = {
        'image1': {'not_build_version': 'some_verson'}
    }
    platform.state_db = mock.MagicMock()
    ppi.logger.error = mock.MagicMock()
    platform.write_sw_network_stack_paths()
    ppi.logger.error.assert_has_calls([
        mock.call(f"Unable to find 'build_version' in {ppi.VERSION_FILE}"),
        mock.call(
            f"Unable to find 'build_version' in image1:{ppi.VERSION_FILE}"
        ),
    ])

  def test_write_sw_os_paths(self):
    platform = ppi.PinsPlatformInit()
    platform.primary_version_dict = {
        'build_version': 'pins_primary_version',
        'kernel_version': '5.10.0-30-2-amd64',
    }
    platform.alternate_versions_dict = {
        'image1': {
            'build_version': 'pins_alternate_version',
            'kernel_version': '5.09.0-30-2-amd64',
        }
    }
    platform.state_db = mock.MagicMock()
    platform.write_sw_os_paths()
    platform.state_db.hset.assert_any_call(
        ppi.SW_COMP_INFO_OS0, ppi.SOFTWARE_VERSION, '5.10.0-30-2-amd64'
    )
    platform.state_db.hset.assert_any_call(
        ppi.SW_COMP_INFO_OS0, ppi.OPER_STATUS, ppi.ACTIVE
    )
    platform.state_db.hset.assert_any_call(
        'SW_COMP_INFO|os1', ppi.SOFTWARE_VERSION, '5.09.0-30-2-amd64'
    )
    platform.state_db.hset.assert_any_call(
        'SW_COMP_INFO|os1', ppi.OPER_STATUS, ppi.INACTIVE
    )

  def test_write_sw_os_paths_no_kernel_version(self):
    platform = ppi.PinsPlatformInit()
    platform.primary_version_dict = {
        'no_kernel_version': 'no_version',
    }
    platform.alternate_versions_dict = {
        'image1': {
            'no_kernel_version': 'no_version',
        }
    }
    platform.state_db = mock.MagicMock()
    ppi.logger.error = mock.MagicMock()
    platform.write_sw_os_paths()
    ppi.logger.error.assert_has_calls([
        mock.call(f"Unable to find '{ppi.KERNEL}' in {ppi.VERSION_FILE}"),
        mock.call(
            f"Unable to find image1:{ppi.VERSION_FILE} or '{ppi.KERNEL}' in"
            f' image1:{ppi.VERSION_FILE}'
        ),
    ])

  def test_write_system_paths(self):
    platform = ppi.PinsPlatformInit()
    platform.state_db = mock.MagicMock()
    with mock.patch.object(
        builtins, 'open', return_value=mock.MagicMock()
    ) as mock_open:
      mock_file = io.StringIO("""
          Syslog config file
          Some string Target="1.2.3.4" Some other string
          Syslog config file
          Some string Target="1.2.3.5" Some other string
      """)
      mock_open.return_value.__enter__.return_value = mock_file
      platform.write_system_paths()
      platform.state_db.hset.assert_has_calls([
          mock.call(f"{ppi.SYSLOG_SERVER}|{'1.2.3.4'}", ppi.HOST, '1.2.3.4'),
          mock.call(f"{ppi.SYSLOG_SERVER}|{'1.2.3.5'}", ppi.HOST, '1.2.3.5'),
      ])

  def test_write_system_paths_no_ip(self):
    platform = ppi.PinsPlatformInit()
    platform.state_db = mock.MagicMock()
    with mock.patch.object(
        builtins, 'open', return_value=mock.MagicMock()
    ) as mock_open:
      mock_file = io.StringIO("""
          Syslog config file
          No IP address
      """)
      mock_open.return_value.__enter__.return_value = mock_file
      ppi.logger.error = mock.MagicMock()
      platform.write_system_paths()
      ppi.logger.error.assert_called_once_with(
          f'Unable to find IP address in {ppi.SYSLOG_CONF} file. Unable to'
          ' extract IP address from line'
      )

  @mock.patch(
      'pins_platform_init.PinsPlatformInit.write_system_paths',
      side_effect=OSError,
  )
  def test_write_system_path_assert_os_error(self, mock_error):
    with self.assertRaises(OSError):
      ppi.PinsPlatformInit().write_system_paths()
    mock_error.assert_called_once()

  def test_get_network_name(self):
    platform = ppi.PinsPlatformInit()
    platform.run_command = mock.MagicMock()
    expected_network_name = 'ju1u1m1.sqs02.net.google.com'
    for hostname in ['ju1u1m1.sqs02.net.google.com', 'ju1u1m1.sqs02']:
      platform.run_command.return_value = hostname
      self.assertEqual(platform.get_network_name(), expected_network_name)

  def test_write_host_paths(self):
    platform = ppi.PinsPlatformInit()
    platform.config_db = mock.MagicMock()
    platform.state_db = mock.MagicMock()
    platform.get_network_name = mock.MagicMock()
    platform.get_network_name.return_value = 'ju1u1m1.sqs02.net.google.com'
    platform.config_db.hget.return_value = 'ju1u1m1.sqs02.net.google.com'
    platform.write_host_paths()
    self.assertEqual(platform.network_name, 'ju1u1m1.sqs02.net.google.com')
    platform.state_db.hset.assert_has_calls([
        mock.call(ppi.HOST_STATS, ppi.HOSTNAME, platform.network_name),
        mock.call(
            ppi.CHASSIS_INFO, ppi.FULLY_QUALIFIED_NAME, platform.network_name
        ),
    ])

  def test_write_cpu_path(self):
    platform = ppi.PinsPlatformInit()
    platform.appl_state_db = mock.MagicMock()
    platform.write_cpu_path_appl_state_db()
    platform.appl_state_db.hset.assert_called_once_with(
        ppi.PORT_TABLE_CPU, 'NULL', 'NULL'
    )

  def test_write_pinsinit_done(self):
    platform = ppi.PinsPlatformInit()
    platform.state_db = mock.MagicMock()
    platform.write_pinsinit_done()
    platform.state_db.hset.assert_called_once_with(
        ppi.PINS_PLATFORM_INIT_TBL,
        ppi.PINS_PLATFORM_READY,
        ppi.PINS_PLATFORM_READY,
    )


if __name__ == '__main__':
  unittest.main(verbosity=2)
