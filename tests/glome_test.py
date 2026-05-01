import filecmp
import os
import sys
import tempfile
import unittest
from unittest import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, 'host_modules')
sys.path.append(host_modules_path)

import glome


class TestGlome(unittest.TestCase):

  payload = '[service]\nkey = key\nkey-version = 1\nurl-prefix = url_prefix\n\n'

  @classmethod
  def setUpClass(cls):
    with mock.patch('glome.Glome.__init__', return_value=None):
      cls.glome_module = glome.Glome(glome.MOD_NAME)

  def setUp(self):
    self.glome_file = tempfile.NamedTemporaryFile(mode='w+', delete=False)
    self.glome_backup_file = tempfile.NamedTemporaryFile(
        mode='w+', delete=False
    )
    glome.Glome._GLOME_PATH = self.glome_file.name
    glome.Glome._GLOME_BACKUP_PATH = self.glome_backup_file.name

  def tearDown(self):
    self.glome_file.close()
    self.glome_backup_file.close()

  def _get_json_payload(self, enabled=True):
    if enabled:
      return (
          '{"enabled": true, "key": "key", "key_version": 1, "url_prefix":'
          ' "url_prefix"}'
      )
    else:
      return '{"enabled": false}'

  def test_push_config_checkpoint_copy(self):
    self.glome_file.write(self.payload)
    self.glome_file.flush()
    result = self.glome_module.push_config(self._get_json_payload())
    self.assertTrue(
        filecmp.cmp(glome.Glome._GLOME_PATH, glome.Glome._GLOME_BACKUP_PATH)
    )
    self.assertEqual(result[0], 0)

  def test_push_config_checkpoint_remove(self):
    os.remove(self.glome_file.name)
    result = self.glome_module.push_config(self._get_json_payload())
    self.assertFalse(os.path.exists(glome.Glome._GLOME_BACKUP_PATH))
    self.assertEqual(result[0], 0)

  def test_push_config_checkpoint_noop(self):
    os.remove(self.glome_file.name)
    os.remove(self.glome_backup_file.name)
    result = self.glome_module.push_config(self._get_json_payload())
    self.assertEqual(result[0], 0)

  def test_push_config_disabled_file_removed(self):
    result = self.glome_module.push_config(self._get_json_payload(False))
    self.assertEqual(result[0], 0)
    self.assertFalse(os.path.exists(glome.Glome._GLOME_PATH))

  def test_push_config_disabled_file_noop(self):
    result = self.glome_module.push_config(self._get_json_payload(False))
    self.assertEqual(result[0], 0)
    self.assertFalse(os.path.exists(glome.Glome._GLOME_PATH))

  def test_push_config_enabled(self):
    result = self.glome_module.push_config(self._get_json_payload())
    self.assertEqual(result[0], 0)
    with open(glome.Glome._GLOME_PATH, 'r') as f:
      self.assertEqual(f.read(), self.payload)

  def test_push_config_error(self):
    result = self.glome_module.push_config('invalid json')
    self.assertNotEqual(result[0], 0)

    with mock.patch('glome.os.path.exists', mock.MagicMock(return_value=True)):
      with mock.patch('glome.shutil') as mock_shutil:
        mock_shutil.copy.side_effect = PermissionError
        result = self.glome_module.push_config(self._get_json_payload())
        self.assertNotEqual(result[0], 0)

    with mock.patch('glome.os.path.exists', mock.MagicMock(return_value=False)):
      with mock.patch('glome.os.remove') as mock_remove:
        mock_remove.side_effect = OSError
        result = self.glome_module.push_config(self._get_json_payload())
        self.assertNotEqual(result[0], 0)

  def test_restore_checkpoint_noop(self):
    os.remove(self.glome_backup_file.name)
    os.remove(self.glome_file.name)
    result = self.glome_module.restore_checkpoint()
    self.assertEqual(result[0], 0)

  def test_restore_checkpoint_copy(self):
    self.glome_backup_file.write(self.payload)
    self.glome_backup_file.flush()
    result = self.glome_module.restore_checkpoint()
    self.assertTrue(
        filecmp.cmp(glome.Glome._GLOME_PATH, glome.Glome._GLOME_BACKUP_PATH)
    )
    self.assertEqual(result[0], 0)

  def test_restore_checkpoint_remove(self):
    os.remove(self.glome_backup_file.name)
    result = self.glome_module.restore_checkpoint()
    self.assertFalse(os.path.exists(glome.Glome._GLOME_PATH))
    self.assertEqual(result[0], 0)

  def test_restore_checkpoint_error(self):
    with mock.patch('glome.os.path.exists', mock.MagicMock(return_value=True)):
      with mock.patch('glome.shutil') as mock_shutil:
        mock_shutil.copy.side_effect = PermissionError
        result = self.glome_module.restore_checkpoint()
        self.assertNotEqual(result[0], 0)

    with mock.patch('glome.os.path.exists', mock.MagicMock(return_value=False)):
      with mock.patch('glome.os.remove') as mock_remove:
        mock_remove.side_effect = OSError
        result = self.glome_module.restore_checkpoint()
        self.assertNotEqual(result[0], 0)
