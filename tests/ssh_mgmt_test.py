"""Tests for ssh_mgmt"""

import builtins
import importlib.util
import importlib.machinery
import os
import pytest
import sys
import json

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

def load_source(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, filename)
    spec = importlib.util.spec_from_file_location(modname, filename, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # The module is always executed and not cached in sys.modules.
    # Uncomment the following line to cache the module.
    sys.modules[module.__name__] = module
    loader.exec_module(module)
    return module

TEST_EXCEPTION_MESSAGE = "test raise exception message"
load_source("host_service", host_modules_path + "/host_service.py")
load_source("ssh_mgmt", host_modules_path + "/ssh_mgmt.py")

from ssh_mgmt import *


class MockFileHandler:

    def __init__(self):
        self.contents = ""

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        pass

    def write(self, content):
        self.contents += content

    def close(self, content):
        pass

    def get_contents(self):
        return self.contents


class TestSshMgmt(object):
    @classmethod
    def setup_class(cls):
        with mock.patch("ssh_mgmt.SshMgmt.__init__", return_value=None):
            cls.ssh_mgmt_module = SshMgmt(MOD_NAME)

    def test_create_checkpoint(self):
        # Create checkpoint succeeds.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                    with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                        with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                            result = self.ssh_mgmt_module.create_checkpoint([])
                            assert result[0] == 0
                            assert result[1] == "Successfully created checkpoint"
                            mock_isdir.assert_called_with(CHECKPOINT_DIR)
                            mock_makedirs.assert_called_with(
                                CHECKPOINT_DIR, exist_ok=True)
                            mock_exists.assert_has_calls([
                                mock.call(
                                    os.path.join(CA_PUB_KEY_DIR,
                                                 CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                            ],
                                any_order=True)
                            mock_remove.assert_called_with(
                                os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE))
                            mock_makedirs.assert_has_calls([
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                            ],
                                any_order=True)
                            mock_shutil.copyfile.assert_has_calls([
                                mock.call(
                                    os.path.join(CA_PUB_KEY_DIR,
                                                 CA_PUB_KEY_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE)),
                            ],
                                any_order=True)
                            mock_shutil.move.assert_has_calls([
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                            ],
                                any_order=True)

        # Create checkpoint succeeds when old checkpoint exists.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                    with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                        with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                            result = self.ssh_mgmt_module.create_checkpoint([])
                            assert result[0] == 0
                            assert result[1] == "Successfully created checkpoint"
                            mock_isdir.assert_called_with(CHECKPOINT_DIR)
                            mock_makedirs.assert_called_with(
                                CHECKPOINT_DIR, exist_ok=True)
                            mock_shutil.rmtree.assert_called_with(
                                CHECKPOINT_DIR)
                            mock_exists.assert_has_calls([
                                mock.call(
                                    os.path.join(CA_PUB_KEY_DIR,
                                                 CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                            ],
                                any_order=True)
                            mock_remove.assert_called_with(
                                os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE))
                            mock_makedirs.assert_has_calls([
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                                mock.call(CHECKPOINT_DIR, exist_ok=True),
                            ],
                                any_order=True)
                            mock_shutil.copyfile.assert_has_calls([
                                mock.call(
                                    os.path.join(CA_PUB_KEY_DIR,
                                                 CA_PUB_KEY_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME),
                                    os.path.join(CHECKPOINT_DIR, COPY_TEMP_FILE))
                            ],
                                any_order=True)
                            mock_shutil.move.assert_has_calls([
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME))
                            ],
                                any_order=True)

        # Create checkpoint succeeds when source files do not exist.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=False)) as mock_exists:
                with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                    result = self.ssh_mgmt_module.create_checkpoint([])
                    assert result[0] == 0
                    assert result[1] == "Successfully created checkpoint"
                    mock_isdir.assert_called_with(CHECKPOINT_DIR)
                    mock_makedirs.assert_called_with(
                        CHECKPOINT_DIR, exist_ok=True)
                    mock_exists.assert_has_calls([
                        mock.call(
                            os.path.join(CA_PUB_KEY_DIR,
                                         CA_PUB_KEY_NAME)),
                        mock.call(
                            os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                         ROOT_AUTHORIZED_KEYS_NAME)),
                        mock.call(
                            os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                         ROOT_AUTHORIZED_USERS_NAME)),
                    ],
                        any_order=True)

        def mock_copyfile(src, dest):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        def mock_rmtree(dir):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        def mock_remove(file):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        # Create checkpoint fails when copy and delete operations fail.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)):
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)):
                with mock.patch("ssh_mgmt.os.makedirs"):
                    with mock.patch("ssh_mgmt.os.remove"):
                        with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                            mock_shutil.copyfile = mock_copyfile
                            mock_shutil.rmtree = mock_rmtree
                            result = self.ssh_mgmt_module.create_checkpoint([])
                            assert result[0] == 1
                            assert result[1] != "Successfully created checkpoint"

        # Create checkpoint fails when old checkpoint exists and fail to delete it.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)):
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)):
                with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                    mock_shutil.rmtree = mock_rmtree
                    result = self.ssh_mgmt_module.create_checkpoint([])
                    assert result[0] == 1
                    assert result[1] != "Successfully created checkpoint"

        # Create checkpoint success when temp file delete fails.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)):
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)):
                with mock.patch("ssh_mgmt.os.makedirs"):
                    with mock.patch("ssh_mgmt.shutil"):
                        os.remove = mock_remove
                        result = self.ssh_mgmt_module.create_checkpoint([])
                        assert result[0] == 0
                        assert result[1] == "Successfully created checkpoint"

    def test_restore_checkpoint(self):
        # Restore checkpoint fails when checkpoint does not exist.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)) as mock_isdir:
            result = self.ssh_mgmt_module.restore_checkpoint([])
            assert result[0] == 1
            assert result[1] == "Checkpoint does not exist"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

        # Restore checkpoint succeeds.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                    with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                        with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                            result = self.ssh_mgmt_module.restore_checkpoint([
                            ])
                            assert result[0] == 0
                            assert result[1] == "Successfully restored checkpoint"
                            mock_isdir.assert_called_with(CHECKPOINT_DIR)
                            mock_exists.assert_has_calls([
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                            ],
                                any_order=True)
                            mock_remove.assert_has_calls([
                                mock.call(os.path.join(
                                    CA_PUB_KEY_DIR, COPY_TEMP_FILE)),
                                mock.call(os.path.join(
                                    PERSISTENT_CA_PUB_KEY_DIRS[0], COPY_TEMP_FILE)),
                                mock.call(os.path.join(
                                    ROOT_AUTHORIZED_KEYS_DIR, COPY_TEMP_FILE)),
                                mock.call(os.path.join(
                                    PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                    COPY_TEMP_FILE)),
                                mock.call(os.path.join(
                                    ROOT_AUTHORIZED_USERS_DIR, COPY_TEMP_FILE)),
                                mock.call(os.path.join(
                                    PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                    COPY_TEMP_FILE))
                            ],
                                any_order=True)
                            mock_makedirs.assert_has_calls([
                                mock.call(CA_PUB_KEY_DIR, exist_ok=True),
                                mock.call(
                                    PERSISTENT_CA_PUB_KEY_DIRS[0], exist_ok=True),
                                mock.call(ROOT_AUTHORIZED_KEYS_DIR,
                                          exist_ok=True),
                                mock.call(
                                    PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0], exist_ok=True),
                                mock.call(ROOT_AUTHORIZED_USERS_DIR,
                                          exist_ok=True),
                                mock.call(
                                    PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0], exist_ok=True),
                            ],
                                any_order=True)
                            mock_shutil.copyfile.assert_has_calls([
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 CA_PUB_KEY_NAME),
                                    os.path.join(CA_PUB_KEY_DIR, COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 CA_PUB_KEY_NAME),
                                    os.path.join(PERSISTENT_CA_PUB_KEY_DIRS[0],
                                                 COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME),
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME),
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                        COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME),
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 COPY_TEMP_FILE)),
                                mock.call(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME),
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                        COPY_TEMP_FILE))
                            ],
                                any_order=True)
                            mock_shutil.move.assert_has_calls([
                                mock.call(
                                    os.path.join(CA_PUB_KEY_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(CA_PUB_KEY_DIR, CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(
                                        PERSISTENT_CA_PUB_KEY_DIRS[0], COPY_TEMP_FILE),
                                    os.path.join(PERSISTENT_CA_PUB_KEY_DIRS[0],
                                                 CA_PUB_KEY_NAME)),
                                mock.call(
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 COPY_TEMP_FILE),
                                    os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                 ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                        COPY_TEMP_FILE),
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                        ROOT_AUTHORIZED_KEYS_NAME)),
                                mock.call(
                                    os.path.join(
                                        ROOT_AUTHORIZED_USERS_DIR, COPY_TEMP_FILE),
                                    os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                 ROOT_AUTHORIZED_USERS_NAME)),
                                mock.call(
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                        COPY_TEMP_FILE),
                                    os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                        ROOT_AUTHORIZED_USERS_NAME))
                            ],
                                any_order=True)
                            mock_shutil.rmtree.assert_called_with(
                                CHECKPOINT_DIR)

        # Restore checkpoint succeeds when source files do not exist.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=False)) as mock_exists:
                with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                    result = self.ssh_mgmt_module.restore_checkpoint([])
                    assert result[0] == 0
                    assert result[1] == "Successfully restored checkpoint"
                    mock_isdir.assert_called_with(CHECKPOINT_DIR)
                    mock_exists.assert_has_calls([
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         CA_PUB_KEY_NAME)),
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         CA_PUB_KEY_NAME)),
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         ROOT_AUTHORIZED_KEYS_NAME)),
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         ROOT_AUTHORIZED_KEYS_NAME)),
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         ROOT_AUTHORIZED_USERS_NAME)),
                        mock.call(
                            os.path.join(CHECKPOINT_DIR,
                                         ROOT_AUTHORIZED_USERS_NAME)),
                    ],
                        any_order=True)
                    mock_shutil.rmtree.assert_called_with(
                        CHECKPOINT_DIR)

        def mock_copyfile(src, dest):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        def mock_rmtree(dir):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        # Restore checkpoint fails when copy and delete operations fail.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)):
                with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                    with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                        mock_shutil.copyfile = mock_copyfile
                        mock_shutil.rmtree = mock_rmtree
                        result = self.ssh_mgmt_module.restore_checkpoint([])
                        assert result[0] == 1
                        assert result[1] != "Successfully restored checkpoint"
                        mock_isdir.assert_called_with(CHECKPOINT_DIR)

    def test_delete_checkpoint(self):
        # Delete checkpoint fails when checkpoint does not exist.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)) as mock_isdir:
            result = self.ssh_mgmt_module.delete_checkpoint([])
            assert result[0] == 1
            assert result[1] == "Checkpoint does not exist"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

        # Delete checkpoint succeeds.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                result = self.ssh_mgmt_module.delete_checkpoint([])
                assert result[0] == 0
                assert result[1] == "Successfully deleted checkpoint"
                mock_isdir.assert_called_with(CHECKPOINT_DIR)
                mock_shutil.rmtree.assert_called_with(
                    CHECKPOINT_DIR)

        def mock_rmtree(dir):
            raise OSError(TEST_EXCEPTION_MESSAGE)

        # Delete checkpoint fails when delete operation fails.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                mock_shutil.rmtree = mock_rmtree
                result = self.ssh_mgmt_module.delete_checkpoint([])
                assert result[0] == 1
                assert result[1] == "Error in deleting checkpoint"
                mock_isdir.assert_called_with(CHECKPOINT_DIR)

    def test_set(self):
        # Set fails without creating checkpoint.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=False)) as mock_isdir:
            result = self.ssh_mgmt_module.set([""])
            assert result[0] == 1
            assert result[1] == "Update ssh config before creating checkpoint"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

        # Set fails with invalid JSON input.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            result = self.ssh_mgmt_module.set(["#$%@"])
            assert result[0] == 1
            assert result[1] == "Invalid JSON"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

        # Set succeeds.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            result = self.ssh_mgmt_module.set(["{}"])
            assert result[0] == 0
            assert result[1] == "Successfully set credentials"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

        # Set succeeds with additional input.
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            result = self.ssh_mgmt_module.set(['{"invalid key":{}}'])
            assert result[0] == 0
            assert result[1] == "Successfully set credentials"
            mock_isdir.assert_called_with(CHECKPOINT_DIR)

    def test_set_ca_pub_key(self):
        f = MockFileHandler()
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("builtins.open", mock.MagicMock(return_value=f)) as mock_open:
                with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                    with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                        with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                            with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                                content = {"SshCaPublicKey": [
                                    "TEST-CERT #1", "TEST-CERT #2"]}
                                input_data = json.dumps(content)
                                result = self.ssh_mgmt_module.set([input_data])
                                assert result[0] == 0
                                assert result[1] == "Successfully set credentials"
                                mock_open.assert_called_with(
                                    os.path.join(CHECKPOINT_DIR,
                                                 CA_PUB_KEY_TEMP),
                                    "w")
                                assert f.get_contents() == """TEST-CERT #1
TEST-CERT #2
"""
                                mock_exists.assert_has_calls([
                                    mock.call(os.path.join(
                                        CHECKPOINT_DIR, CA_PUB_KEY_TEMP)),
                                ],
                                    any_order=True)
                                mock_remove.assert_has_calls([
                                    mock.call(os.path.join(
                                        CA_PUB_KEY_DIR, COPY_TEMP_FILE)),
                                    mock.call(os.path.join(
                                        PERSISTENT_CA_PUB_KEY_DIRS[0], COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_makedirs.assert_has_calls([
                                    mock.call(CA_PUB_KEY_DIR, exist_ok=True),
                                    mock.call(
                                        PERSISTENT_CA_PUB_KEY_DIRS[0], exist_ok=True),
                                ],
                                    any_order=True)
                                mock_shutil.copyfile.assert_has_calls([
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     CA_PUB_KEY_TEMP),
                                        os.path.join(CA_PUB_KEY_DIR, COPY_TEMP_FILE)),
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     CA_PUB_KEY_TEMP),
                                        os.path.join(PERSISTENT_CA_PUB_KEY_DIRS[0],
                                                     COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_shutil.move.assert_has_calls([
                                    mock.call(
                                        os.path.join(CA_PUB_KEY_DIR,
                                                     COPY_TEMP_FILE),
                                        os.path.join(CA_PUB_KEY_DIR,
                                                     CA_PUB_KEY_NAME)),
                                    mock.call(
                                        os.path.join(
                                            PERSISTENT_CA_PUB_KEY_DIRS[0],
                                            COPY_TEMP_FILE),
                                        os.path.join(PERSISTENT_CA_PUB_KEY_DIRS[0],
                                                     CA_PUB_KEY_NAME))
                                ],
                                    any_order=True)

    def test_set_account_keys(self):
        f = MockFileHandler()
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("builtins.open", mock.MagicMock(return_value=f)) as mock_open:
                with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                    with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                        with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                            with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                                content = {
                                    "SshAccountKeys": [{
                                        "account":
                                            "root",
                                        "keys": [{
                                            "key":
                                                "Authorized-key #1",
                                            "options": [{
                                                "name": "from",
                                                "value": "*.sales.example.net,!pc.sales.example.net"
                                            }]
                                        }, {
                                            "key": "Authorized-key #2",
                                            "options": []
                                        }, {
                                            "key":
                                                "Authorized-key #3",
                                            "options": [{
                                                "name": "from",
                                                "value": "*.sales.example.net,!pc.sales.example.net"
                                            }, {
                                                "name": "no-port-forwarding"
                                            }]
                                        }]
                                    }, {
                                        "account":
                                            "root",
                                    }, {
                                        "account":
                                            "root",
                                        "keys": [{
                                        }]
                                    }, {
                                        "account":
                                            "non-root",
                                        "keys": [{
                                            "key":
                                                "Non-root account key"
                                        }]
                                    }]
                                }
                                input_data = json.dumps(content)
                                result = self.ssh_mgmt_module.set([input_data])
                                assert result[0] == 0
                                assert result[1] == "Successfully set credentials"
                                builtins.open.assert_called_with(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_KEYS_TEMP), "w")
                                assert f.get_contents() == """from="*.sales.example.net,!pc.sales.example.net" Authorized-key #1
Authorized-key #2
from="*.sales.example.net,!pc.sales.example.net",no-port-forwarding Authorized-key #3
"""
                                mock_exists.assert_has_calls([
                                    mock.call(os.path.join(
                                        CHECKPOINT_DIR, ROOT_AUTHORIZED_KEYS_TEMP)),
                                ],
                                    any_order=True)
                                mock_remove.assert_has_calls([
                                    mock.call(os.path.join(
                                        ROOT_AUTHORIZED_KEYS_DIR, COPY_TEMP_FILE)),
                                    mock.call(os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                        COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_makedirs.assert_has_calls([
                                    mock.call(
                                        ROOT_AUTHORIZED_KEYS_DIR, exist_ok=True),
                                    mock.call(
                                        PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0], exist_ok=True),
                                ],
                                    any_order=True)
                                mock_shutil.copyfile.assert_has_calls([
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     ROOT_AUTHORIZED_KEYS_TEMP),
                                        os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                     COPY_TEMP_FILE)),
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     ROOT_AUTHORIZED_KEYS_TEMP),
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                            COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_shutil.move.assert_has_calls([
                                    mock.call(
                                        os.path.join(
                                            ROOT_AUTHORIZED_KEYS_DIR, COPY_TEMP_FILE),
                                        os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                                     ROOT_AUTHORIZED_KEYS_NAME)),
                                    mock.call(
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                            COPY_TEMP_FILE),
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS[0],
                                            ROOT_AUTHORIZED_KEYS_NAME))
                                ],
                                    any_order=True)

    def test_set_account_users(self):
        f = MockFileHandler()
        with mock.patch("ssh_mgmt.os.path.isdir", mock.MagicMock(return_value=True)) as mock_isdir:
            with mock.patch("builtins.open", mock.MagicMock(return_value=f)) as mock_open:
                with mock.patch("ssh_mgmt.os.path.exists", mock.MagicMock(return_value=True)) as mock_exists:
                    with mock.patch("ssh_mgmt.os.remove") as mock_remove:
                        with mock.patch("ssh_mgmt.os.makedirs") as mock_makedirs:
                            with mock.patch("ssh_mgmt.shutil") as mock_shutil:
                                content = {
                                    "SshAccountUsers": [{
                                        "account":
                                            "root",
                                        "users": [{
                                            "name":
                                                "alice",
                                            "options": [{
                                                "name": "from",
                                                "value": "*.sales.example.net,!pc.sales.example.net"
                                            }]
                                        }, {
                                            "name": "bob",
                                            "options": []
                                        }, {
                                            "name":
                                                "carol",
                                            "options": [{
                                                "name": "from",
                                                "value": "*.sales.example.net,!pc.sales.example.net"
                                            }, {
                                                "name": "no-port-forwarding",
                                                "value": ""
                                            }, {
                                                "value": "option without name"
                                            }]
                                        }]
                                    }, {
                                        "account":
                                            "root",
                                    }, {
                                        "account":
                                            "root",
                                        "users": [{
                                        }]
                                    }, {
                                        "account":
                                            "non-root",
                                        "users": [{
                                            "name":
                                                "non-root-user"
                                        }]
                                    }]
                                }
                                input_data = json.dumps(content)
                                result = self.ssh_mgmt_module.set([input_data])
                                assert result[0] == 0
                                assert result[1] == "Successfully set credentials"
                                builtins.open.assert_called_with(
                                    os.path.join(CHECKPOINT_DIR,
                                                 ROOT_AUTHORIZED_USERS_TEMP), "w")
                                assert f.get_contents() == """from="*.sales.example.net,!pc.sales.example.net" alice
bob
from="*.sales.example.net,!pc.sales.example.net",no-port-forwarding carol
"""
                                mock_exists.assert_has_calls([
                                    mock.call(os.path.join(
                                        CHECKPOINT_DIR, ROOT_AUTHORIZED_USERS_TEMP)),
                                ],
                                    any_order=True)
                                mock_remove.assert_has_calls([
                                    mock.call(os.path.join(
                                        ROOT_AUTHORIZED_USERS_DIR, COPY_TEMP_FILE)),
                                    mock.call(os.path.join(
                                        PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                        COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_makedirs.assert_has_calls([
                                    mock.call(
                                        ROOT_AUTHORIZED_USERS_DIR, exist_ok=True),
                                    mock.call(
                                        PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0], exist_ok=True),
                                ],
                                    any_order=True)
                                mock_shutil.copyfile.assert_has_calls([
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     ROOT_AUTHORIZED_USERS_TEMP),
                                        os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                     COPY_TEMP_FILE)),
                                    mock.call(
                                        os.path.join(CHECKPOINT_DIR,
                                                     ROOT_AUTHORIZED_USERS_TEMP),
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                            COPY_TEMP_FILE))
                                ],
                                    any_order=True)
                                mock_shutil.move.assert_has_calls([
                                    mock.call(
                                        os.path.join(
                                            ROOT_AUTHORIZED_USERS_DIR, COPY_TEMP_FILE),
                                        os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                                     ROOT_AUTHORIZED_USERS_NAME)),
                                    mock.call(
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                            COPY_TEMP_FILE),
                                        os.path.join(
                                            PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS[0],
                                            ROOT_AUTHORIZED_USERS_NAME))
                                ],
                                    any_order=True)

    def test_copy_files_failure(self):
        result = self.ssh_mgmt_module._copy_files(["a", "b"], ["a"])
        assert result[0] == 1
        assert result[1] == "Length of src and dest do not match in _copy_files"

    def test_register(self):
        result = register()
        assert result[0] == SshMgmt
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
