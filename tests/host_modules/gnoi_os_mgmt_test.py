"""Tests for sonic-host-service.host_modules.gnoi_os_mgmt."""
import importlib.machinery
import importlib.util
import os
import json
import pathlib
import pytest
import subprocess
import sys
from unittest import mock

# Base paths relative to the location of this test file
test_path = pathlib.Path(__file__).resolve().parent
sonic_host_service_path = test_path.parent.parent

# Corrected sub-paths
mocks_path = sonic_host_service_path / 'tests' / 'mocks'
host_modules_path = sonic_host_service_path / 'host_modules'
os_mgmt_path = sonic_host_service_path / 'os_mgmt'

print("=== DEBUG INFO ===")
print(f"test_path = {test_path}")
print(f"sonic_host_service_path = {sonic_host_service_path}")
print(f"mocks_path = {mocks_path}")
print(f"host_modules_path = {host_modules_path}")
print(f"os_mgmt_path = {os_mgmt_path}")
print("==================")

# Verify that the expected files exist
print("Verifying file existence:")
print(f"{mocks_path}/host_service.py: {os.path.exists(os.path.join(mocks_path, 'host_service.py'))}")
print(f"{os_mgmt_path}/gnoi_os_proto_defs.py: {os.path.exists(os.path.join(os_mgmt_path, 'gnoi_os_proto_defs.py'))}")
print(f"{host_modules_path}/infra_host.py: {os.path.exists(os.path.join(host_modules_path, 'infra_host.py'))}")
print(f"{host_modules_path}/gnoi_os_mgmt.py: {os.path.exists(os.path.join(host_modules_path, 'gnoi_os_mgmt.py'))}")

# Load the required modules explicitly
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

host_service = load_module('host_service', mocks_path / 'host_service.py')
gnoi_os_proto_defs = load_module('gnoi_os_proto_defs', os_mgmt_path / 'gnoi_os_proto_defs.py')
infra_host = load_module('infra_host', host_modules_path / 'infra_host.py')
sys.modules['host_service'] = host_service
sys.modules['infra_host'] = infra_host
sys.modules['gnoi_os_proto_defs'] = gnoi_os_proto_defs
gnoi_os_mgmt = load_module('gnoi_os_mgmt', host_modules_path / 'gnoi_os_mgmt.py')
sys.modules['gnoi_os_mgmt'] = gnoi_os_mgmt

import gnoi_os_proto_defs as ospb
from gnoi_os_mgmt import COMPONENT_STATE_MINOR
from gnoi_os_mgmt import *

FAKE_VERSION = 'fake_version'
ACTIVE_VERSION = 'active_version'
INACTIVE_VERSION = 'inactive_version'


def create_empty_dir(dirpath):
  """Creates an empty directory in the test filesystem."""
  try:
    path_obj = pathlib.Path(dirpath)
    path_obj.mkdir(parents=True, exist_ok=True)
    return True
  except IOError:
    return False


def create_data_file(filepath, content):
  """Creates a file with the given content."""
  try:
    path_obj = pathlib.Path(filepath)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w') as f:
      f.write(content)
    return True
  except IOError as e:
    print(e)
    return False


def assert_contains_matching_output(filepath, matcher_fn):
  contents = ''
  with open(filepath) as out_file:
    contents = out_file.read()
  assert matcher_fn(contents)


class TestsGnoiOsMgmt(object):

  @classmethod
  def setup_class(cls):
    with mock.patch('gnoi_os_mgmt.super') as mock_host_module:
      cls.gnoi_os_mgmt_module = GnoiOsMgmt(MOD_NAME)

  def test_get_dict_invalid_json(self):
    result = self.gnoi_os_mgmt_module._get_dict('invalid json')
    assert result is None

  def test_get_dict_success(self):
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module._get_dict(json.dumps(input_dict))
    assert result == {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }

  def test_install_invalid_json(self):
    result = self.gnoi_os_mgmt_module.install('invalid json')
    assert result[0] == 1
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  def test_install_no_fields(self):
    result = self.gnoi_os_mgmt_module.install(
        json.dumps({'fake_field': 'fake_value'}))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  def test_install_transfer_missing_version(self):
    input_dict = {ospb.TRANSFER_REQUEST_FIELD: {}}
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  def test_install_invalid_version(self):
    BAD_VERSIONS = ['', '/', 'dir/file', '..']
    for version in BAD_VERSIONS:
      input_dict = {ospb.TRANSFER_REQUEST_FIELD: {ospb.VERSION_FIELD: version}}
      result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

      assert result[0] == 0
      response = json.loads(result[1])
      assert ospb.INSTALL_ERROR_FIELD in response
      assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  @mock.patch('gnoi_os_mgmt.InstallManagerThread')
  def test_install_already_in_progress(self, mock_im_thread):
    self.gnoi_os_mgmt_module.install_manager_thread = mock_im_thread
    mock_im_thread.is_finished.return_value = False

    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response
    assert response[ospb.INSTALL_ERROR_FIELD][
        'type'] == ospb.INSTALL_ERROR_INSTALL_IN_PROGRESS
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

    mock_im_thread.is_finished.assert_called_once()
    self.gnoi_os_mgmt_module.install_manager_thread = None

  def test_install_transfer_works(self):
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.TRANSFER_READY_FIELD in response
    assert self.gnoi_os_mgmt_module.image_receipt_in_progress
    assert self.gnoi_os_mgmt_module.version_in_progress == FAKE_VERSION

  def test_install_transfer_discards_state(self):
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    assert self.gnoi_os_mgmt_module.image_receipt_in_progress
    assert self.gnoi_os_mgmt_module.version_in_progress == FAKE_VERSION

    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: 'fake_version2'
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.TRANSFER_READY_FIELD in response
    assert self.gnoi_os_mgmt_module.image_receipt_in_progress
    assert self.gnoi_os_mgmt_module.version_in_progress == 'fake_version2'

  def test_install_transfer_discards_file(self, fs):
    fake_target_file = os.path.join(FE_TRANSFER_DIR, FAKE_VERSION)
    create_data_file(fake_target_file, 'fake contents')
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.TRANSFER_READY_FIELD in response
    assert self.gnoi_os_mgmt_module.image_receipt_in_progress
    assert self.gnoi_os_mgmt_module.version_in_progress == FAKE_VERSION

    assert not os.path.exists(fake_target_file)

  def test_install_transfer_content_fails(self):
    input_dict = {ospb.TRANSFER_CONTENT_FIELD: {}}
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response

  def test_install_transfer_end_not_in_progress(self):
    input_dict = {ospb.TRANSFER_END_FIELD: {}}
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  def test_install_transfer_end_missing_file(self):
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.TRANSFER_READY_FIELD in response

    input_dict = {ospb.TRANSFER_END_FIELD: {}}
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.INSTALL_ERROR_FIELD in response
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

  def test_install_transfer_seq(self, fs):
    fake_target_file = os.path.join(FE_TRANSFER_DIR, FAKE_VERSION)
    create_data_file(fake_target_file, 'fake contents')
    input_dict = {
        ospb.TRANSFER_REQUEST_FIELD: {
            ospb.VERSION_FIELD: FAKE_VERSION
        }
    }
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.TRANSFER_READY_FIELD in response
    assert self.gnoi_os_mgmt_module.image_receipt_in_progress
    assert self.gnoi_os_mgmt_module.version_in_progress == FAKE_VERSION

    # Data transfer is handled by the gNOI FE. Put the file in place to pretend
    # that occurred.
    real_contents = 'real contents'
    create_data_file(fake_target_file, real_contents)

    input_dict = {ospb.TRANSFER_END_FIELD: {}}
    result = self.gnoi_os_mgmt_module.install(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.VALIDATED_FIELD in response
    assert not self.gnoi_os_mgmt_module.image_receipt_in_progress

    def has_real_contents(contents: str):
      return contents == real_contents

    assert_contains_matching_output(fake_target_file, has_real_contents)

  def test_activate_invalid_json(self):
    result = self.gnoi_os_mgmt_module.activate('invalid json')
    assert result[0] == 1

  @mock.patch('gnoi_os_mgmt.InstallManagerThread')
  def test_activate_in_progress(self, mock_im_thread):
    self.gnoi_os_mgmt_module.install_manager_thread = mock_im_thread
    mock_im_thread.is_finished.return_value = False

    input_dict = {ospb.VERSION_FIELD: FAKE_VERSION}
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_ERROR_FIELD in response

    mock_im_thread.is_finished.assert_called_once()
    self.gnoi_os_mgmt_module.install_manager_thread = None

  def test_activate_no_version(self):
    input_dict = {ospb.NO_REBOOT_FIELD: 'true'}
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_ERROR_FIELD in response

  def test_activate_invalid_version(self):
    BAD_VERSIONS = ['', '/', 'dir/file', '..']
    for version in BAD_VERSIONS:
      input_dict = {
          ospb.VERSION_FIELD: version,
          ospb.STANDBY_SUPERVISOR_FIELD: False
      }
      result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

      assert result[0] == 0
      response = json.loads(result[1])
      assert ospb.ACTIVATE_ERROR_FIELD in response
      assert ospb.DETAIL_FIELD in response[ospb.ACTIVATE_ERROR_FIELD]

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_activate_active_stack_rollback(self, mock_get_version):
    mock_get_version.return_value = ACTIVE_VERSION

    input_dict = {
        ospb.VERSION_FIELD: 'rollback_active_version',
        ospb.STANDBY_SUPERVISOR_FIELD: False
    }
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_OK_FIELD in response

    mock_get_version.assert_called_once_with(True)

  @mock.patch('gnoi_os_mgmt.StackRollbackThread')
  @mock.patch('gnoi_os_mgmt.get_version')
  def test_activate_inactive_stack_rollback(self, mock_get_version,
                                            mock_rb_thread):

    def fake_get_version(active: bool):
      return ACTIVE_VERSION if active else INACTIVE_VERSION

    mock_get_version.side_effect = fake_get_version

    input_dict = {
        ospb.VERSION_FIELD: 'rollback_inactive_version',
        ospb.STANDBY_SUPERVISOR_FIELD: False
    }
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_OK_FIELD in response

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])
    mock_rb_thread.assert_called_once()

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_activate_invalid_stack_rollback(self, mock_get_version):

    def fake_get_version(active: bool):
      return ACTIVE_VERSION if active else INACTIVE_VERSION

    mock_get_version.side_effect = fake_get_version

    input_dict = {ospb.VERSION_FIELD: 'rollback_FAKE_VERSION'}
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_ERROR_FIELD in response

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])

  def test_activate_missing_file(self, fs):
    input_dict = {ospb.VERSION_FIELD: FAKE_VERSION}
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_ERROR_FIELD in response

  @pytest.mark.parametrize('version_name', [FAKE_VERSION, 'spaces in name'])
  @pytest.mark.parametrize('active_side', [True, False, None])
  @mock.patch('gnoi_os_mgmt.InstallManagerThread')
  def test_activate_active_side(self, mock_im_thread, active_side, version_name,
                                fs):
    create_data_file(
        os.path.join(FE_TRANSFER_DIR, version_name), 'fake IM input')
    create_data_file(ACTIVE_SIDE_OUTPUT_LOCATION, 'fake active contents')
    create_data_file(INACTIVE_SIDE_OUTPUT_LOCATION, 'fake inactive contents')

    input_dict = {
        ospb.VERSION_FIELD: version_name,
    }
    # active_side == None to test that not having the field in the input
    # defaults to the active side.
    if active_side is not None:
      input_dict[ospb.STANDBY_SUPERVISOR_FIELD] = not active_side
    result = self.gnoi_os_mgmt_module.activate(json.dumps(input_dict))

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.ACTIVATE_OK_FIELD in response

    assert not os.path.exists(ACTIVE_SIDE_OUTPUT_LOCATION)
    assert not os.path.exists(INACTIVE_SIDE_OUTPUT_LOCATION)
    mock_im_thread.assert_called_with(
        os.path.join(FE_TRANSFER_DIR, version_name),
        True if active_side is None else active_side)

  def test_verify_invalid_json(self):
    result = self.gnoi_os_mgmt_module.install('invalid json')
    assert result[0] == 1

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_verify_active_version(self, mock_get_version):

    def fake_get_version(active: bool):
      return ACTIVE_VERSION if active else ''

    mock_get_version.side_effect = fake_get_version
    result = self.gnoi_os_mgmt_module.verify('{}')

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.VERSION_FIELD in response

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_verify_active_install_manager_output(self, mock_get_version, fs):
    create_data_file(ACTIVE_SIDE_OUTPUT_LOCATION,
                     'install_manager active output')

    def fake_get_version(active: bool):
      return '' if active else ''

    mock_get_version.side_effect = fake_get_version
    result = self.gnoi_os_mgmt_module.verify('{}')

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.VERIFY_RESPONSE_FAIL_MESSAGE in response

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_verify_inactive_version(self, mock_get_version, fs):

    def fake_get_version(active: bool):
      return '' if active else 'FAKE_VERSION_2'

    mock_get_version.side_effect = fake_get_version

    result = self.gnoi_os_mgmt_module.verify('{}')

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.VERSION_FIELD in response[ospb.VERIFY_RESPONSE_STANDBY][
        ospb.VERIFY_STANDBY_RESPONSE]

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])

  @mock.patch('gnoi_os_mgmt.get_version')
  def test_verify_inactive_install_manager_output(self, mock_get_version, fs):
    create_data_file(INACTIVE_SIDE_OUTPUT_LOCATION,
                     'install_manager inactive output')

    def fake_get_version(active: bool):
      return '' if active else ''

    mock_get_version.side_effect = fake_get_version
    result = self.gnoi_os_mgmt_module.verify('{}')

    assert result[0] == 0
    response = json.loads(result[1])
    assert ospb.VERIFY_RESPONSE_FAIL_MESSAGE in response[
        ospb.VERIFY_RESPONSE_STANDBY][ospb.VERIFY_STANDBY_RESPONSE]

    mock_get_version.assert_has_calls(
        [mock.call.exists(True),
         mock.call.exists(False)])

  def test_register(self):
    result = register()
    assert result[0] == GnoiOsMgmt
    assert result[1] == MOD_NAME

  @classmethod
  def teardown_class(cls):
    print('TEARDOWN')


class TestsInstallManagerThread(object):

  @classmethod
  def setup_class(cls):
    cls.mock_helper = mock.Mock()
    cls.im_thread = InstallManagerThread('fake_file', False)
    # Override the component_state with mock
    cls.im_thread.component_state = cls.mock_helper

  def test_is_finished(self):
    self.im_thread.status = RunningStatus.NOT_STARTED
    assert not self.im_thread.is_finished()
    self.im_thread.status = RunningStatus.RUNNING
    assert not self.im_thread.is_finished()
    self.im_thread.status = RunningStatus.FINISHED
    assert self.im_thread.is_finished()

  # run invokes command and exits
  @mock.patch('infra_host.InfraHost._run_command')
  @mock.patch('gnoi_os_mgmt.get_version')
  @mock.patch('redis.Redis')
  def test_run(self, mock_redis, mock_get_version, mock_run_command):

    mock_run_command.return_value = (0, ['stdout: execute IM'],
                                     ['stderror: execute IM'])
    mock_get_version.return_value = FAKE_VERSION
    # This looks odd, but this returns the mock instance from the
    # constructor, so the later call to hset
    mock_redis.return_value = mock_redis
    mock_redis.hset.return_value = None

    _ = self.im_thread.run()

    mock_run_command.assert_called_once()
    mock_get_version.assert_called_once()
    mock_redis.assert_called_once()
    mock_redis.hset.assert_called_once_with(SW_COMP_INFO_STACK1,
                                            SOFTWARE_VERSION, FAKE_VERSION)

  @mock.patch('infra_host.InfraHost._run_command')
  @mock.patch('gnoi_os_mgmt.os.path')
  @mock.patch('gnoi_os_mgmt.GnoiOsMgmt')
  @mock.patch('redis.Redis')
  def test_run_fail(self, mock_redis, mock_os_mgmt, mock_os_path,
                    mock_run_command):
    mock_run_command.return_value = (1, ['stdout: execute IM'],
                                     ['stderror: execute IM'])

    _ = self.im_thread.run()

    mock_run_command.assert_called_once()
    mock_os_path.exists.assert_not_called()
    mock_os_mgmt.get_version_from_file.assert_not_called()
    mock_redis.assert_not_called()
    self.mock_helper.ReportComponentState.assert_called_once_with(
        gnoi_os_mgmt.COMPONENT_STATE_MINOR, mock.ANY)

  @mock.patch('infra_host.InfraHost._run_command')
  @mock.patch('gnoi_os_mgmt.get_version')
  @mock.patch('redis.Redis')
  def test_run_spaces_in_version(self, mock_redis, mock_get_version,
                                 mock_run_command, fs):
    mock_run_command.return_value = (0, ['stdout: execute IM'],
                                     ['stderror: execute IM'])
    mock_get_version.return_value = 'spaces in name'
    # This looks odd, but this returns the mock instance from the
    # constructor, so the later call to hset
    mock_redis.return_value = mock_redis
    mock_redis.hset.return_value = None

    _ = self.im_thread.run()

    mock_run_command.assert_called_once()
    mock_get_version.assert_called_once_with(False)
    mock_redis.assert_called_once()
    mock_redis.hset.assert_called_once_with(SW_COMP_INFO_STACK1,
                                            SOFTWARE_VERSION, 'spaces in name')

  @classmethod
  def teardown_class(cls):
    print('TEARDOWN')


class TestsGetVersion(object):

  @classmethod
  def setup_class(cls):
    print('SETUP')

  @mock.patch('subprocess.run')
  def test_get_active_version(self, mock_subprocess):
    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b'''Current: SONiC-OS-daily_20250530_14_RC00
Next: SONiC-OS-daily_20250602_14_RC00
Available:
SONiC-OS-daily_20250602_14_RC00
SONiC-OS-daily_20250530_14_RC00''',
        stderr='')
    assert get_version(True) == 'daily_20250530_14_RC00'

  @mock.patch('subprocess.run')
  def test_get_active_version_empty(self, mock_subprocess):
    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout=b'''''', stderr='')
    assert get_version(True) == ''

    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout=b'''''', stderr='')
    assert get_version(True) == ''

  @mock.patch('subprocess.run')
  def test_get_inactive_version(self, mock_subprocess):
    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=b'daily_20250602_14_RC00',
        stderr='')
    assert get_version(False) == 'daily_20250602_14_RC00'

  @mock.patch('subprocess.run')
  def test_get_inactive_version_empty(self, mock_subprocess):
    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout=b'', stderr='')
    assert get_version(False) == ''

    mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b'', stderr='')
    assert get_version(False) == ''

  @classmethod
  def teardown_class(cls):
    print('TEARDOWN')
