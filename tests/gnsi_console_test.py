"""Tests for gnsi_console."""

import importlib.util
import importlib.machinery
import json
import sys
import os
import pytest

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    # Expect the 'mock' package for python 2
    # https://pypi.python.org/pypi/mock
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

TEST_EXCEPTION_MESSAGE = "test raise exception message"
TEST_HASHED_PASSWORD = "$6$wNa3DzanMzQ6U.0x$2LAaCYaiAua9muP/Q04sKWMNpHnIOOu2rQ.il.3BOjeKTrxCMqwg2NIamWmhhw3HZgHZGb79RozrKVc.tDnLs1"
TEST_TEXT_PASSWORD = "some_test_password"
TEST_VALID_USER = "root"
TEST_INVALID_USER = "root_test"
TEST_OLD_PASSWORD_FILE_CONTENT = [
    "root:old_hashed_password:12215:0:99999:7:::\n",
    "second_user:second_hashed_password:12215:0:99999:7:::\n"
]
TEST_UPDATED_PASSWORD_FILE_CONTENT = [
    TEST_VALID_USER + ":" + TEST_HASHED_PASSWORD + ":12215:0:99999:7:::\n",
    TEST_OLD_PASSWORD_FILE_CONTENT[1]
]
TEST_VALID_PASSWORD_CHANGE_REQEST = (
    "{ \"ConsolePasswords\": [ { \"name\": \"root\", \"password\" : "
    "\"new_root_text_password\" }, { \"name\": \"second_user\", \"password\" :"
    " \"new_second_text_password\"}]}"
)
TEST_INVALID_PASSWORD_CHANGE_REQEST = (
    "\"ConsolePasswords\": "
    "[ { \"name\": \"root\", \"password\" : \"new_root_text_password\" }, "
    "{ \"name\": \"second_user\", \"password\" : \"new_second_text_password\"}]"
)
TEST_RANDOM_PASSWORD_CHANGE_REQEST = (
    "{ \"Random\": [ { \"name\": \"root\", \"password\" : "
    "\"new_root_text_password\" }, { \"name\": \"second_user\", \"password\" :"
    " \"new_second_text_password\"}]}"
)

def load_source(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, filename)
    spec = importlib.util.spec_from_file_location(modname, filename, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # The module is always executed and not cached in sys.modules.
    # Uncomment the following line to cache the module.
    sys.modules[module.__name__] = module
    loader.exec_module(module)
    return module

load_source("host_service", host_modules_path + "/host_service.py")
#load_source("infra_host", host_modules_path + "/infra_host.py")
load_source("gnsi_console", host_modules_path + "/gnsi_console.py")

from gnsi_console import *


class TestGnsiConsole(object):
    @classmethod
    def setup_class(cls):
        with mock.patch("gnsi_console.GnsiConsole.__init__", return_value=None):
            cls.gnsi_console_module = GnsiConsole(MOD_NAME)

    def test_create_checkpoint_success(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            result = self.gnsi_console_module.create_checkpoint([""])
            assert result[0] == 0
            assert result[1] == "Successfully created checkpoint"
            mock_shutil.copy.assert_called_once_with(PASSWD_FILE, PASSWD_FILE_CHECKPOINT_FILE)

    def raise_exception_shutil_test(self, src, dst):
        raise OSError(TEST_EXCEPTION_MESSAGE)

    def test_create_checkpoint_fail(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            mock_shutil.copy = self.raise_exception_shutil_test
            result = self.gnsi_console_module.create_checkpoint([""])
            assert result[0] == 1
            assert result[1] == "Failed to create checkpoint with error: " + TEST_EXCEPTION_MESSAGE

    def test_restore_checkpoint_fail_checkpoint_not_present(self):
        with mock.patch("gnsi_console.os") as mock_os:
            mock_os.path.isfile.return_value = False
            result = self.gnsi_console_module.restore_checkpoint([""])
            assert result[0] == 1
            assert result[1] == "Checkpoint file is not present"
            mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def test_restore_checkpoint_success(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.update_password_file") as mock_update_password_file:
                mock_os.path.isfile.return_value = True
                mock_update_password_file.return_value = (0, "Successfully updated console passwords")
                result = self.gnsi_console_module.restore_checkpoint([""])
                assert result[0] == 0
                assert result[1] == "restore_checkpoint: Successfully updated console passwords"
                mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                mock_update_password_file.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def raise_exception_os_test(self, src):
        raise OSError(TEST_EXCEPTION_MESSAGE)

    def test_delete_checkpoint_success(self):
        with mock.patch("gnsi_console.os") as mock_os:
            result = self.gnsi_console_module.delete_checkpoint([""])
            assert result[0] == 0
            assert result[1] == "Successfully deleted checkpoint"
            mock_os.remove.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def test_delete_checkpoint_fail(self):
        with mock.patch("gnsi_console.os") as mock_os:
            mock_os.remove = self.raise_exception_os_test
            result = self.gnsi_console_module.delete_checkpoint([""])
            assert result[0] == 1
            assert result[1] == "Failed to delete checkpoint with error: " + TEST_EXCEPTION_MESSAGE

    def test_get_hashed_password_success(self):
        with mock.patch("gnsi_console._run_command") as mock_run_command:
            mock_run_command.return_value = (0, [TEST_HASHED_PASSWORD], [])
            assert self.gnsi_console_module.get_hashed_password(TEST_TEXT_PASSWORD) == TEST_HASHED_PASSWORD
            mock_run_command.assert_called_once_with(OPENSSL_COMMAND + TEST_TEXT_PASSWORD)

    def test_get_hashed_password_fail(self):
        with mock.patch("gnsi_console._run_command") as mock_run_command:
            with mock.patch("gnsi_console.logger.error") as mock_logerror:
              mock_run_command.return_value = (1, ["stdout test message"], ["stderr test message"])
              assert not self.gnsi_console_module.get_hashed_password(TEST_TEXT_PASSWORD)
              expected_log_message = "gnsi_console: Failed to get hash for given text password " \
                                     "with stdout: ['stdout test message'], " \
                                     "stderr: ['stderr test message']"
              mock_logerror.assert_called_once_with(expected_log_message)
              mock_run_command.assert_called_once_with(OPENSSL_COMMAND + TEST_TEXT_PASSWORD)


    def test_read_password_file_success(self):
        with mock.patch("gnsi_console.open") as mock_open:
            mock_file_handler = mock_open.return_value.__enter__.return_value
            mock_file_handler.readlines.return_value = TEST_OLD_PASSWORD_FILE_CONTENT.copy()
            result = self.gnsi_console_module.read_password_file()
            assert result[0] == TEST_OLD_PASSWORD_FILE_CONTENT
            assert not result[1]
            mock_file_handler.readlines.assert_called_once_with()

    def raise_ioerror_fh(self):
        raise IOError("IOError in unit test")

    def test_read_password_file_fail(self):
        with mock.patch("gnsi_console.open") as mock_open:
            mock_file_handler = mock_open.return_value.__enter__.return_value
            mock_file_handler.readlines = self.raise_ioerror_fh
            result = self.gnsi_console_module.read_password_file()
            assert not result[0]
            assert result[1] == "Failed to read password file with error: " + "IOError in unit test"

    def test_update_password_if_user_found_success(self):
        test_password_file_content = TEST_OLD_PASSWORD_FILE_CONTENT.copy()
        self.gnsi_console_module.update_password_if_user_found(TEST_VALID_USER,
                                                               TEST_HASHED_PASSWORD,
                                                               test_password_file_content)
        assert test_password_file_content == TEST_UPDATED_PASSWORD_FILE_CONTENT

    def test_update_password_if_user_found_fail(self):
        with mock.patch("gnsi_console.logger.error") as mock_logerror:
            test_password_file_content = TEST_OLD_PASSWORD_FILE_CONTENT.copy()
            self.gnsi_console_module.update_password_if_user_found(TEST_INVALID_USER,
                                                                   TEST_HASHED_PASSWORD,
                                                                   test_password_file_content)
            assert test_password_file_content == TEST_OLD_PASSWORD_FILE_CONTENT
            mock_logerror.assert_called_once_with("gnsi_console: The given user name: %s does "
                                                  "not exist in the password file"
                                                  % TEST_INVALID_USER)

    def raise_ioerror_fh_with_one_arg(self, first_arg):
        raise IOError("IOError in unit test")

    def test_create_temp_passwd_file_success(self):
        with mock.patch("gnsi_console.open") as mock_open:
            mock_file_handler = mock_open.return_value.__enter__.return_value
            result = self.gnsi_console_module.create_temp_passwd_file(
                TEST_UPDATED_PASSWORD_FILE_CONTENT.copy())
            assert result[0] == 0
            assert result[1] == ""
            mock_file_handler.writelines.assert_called_once_with(
                TEST_UPDATED_PASSWORD_FILE_CONTENT)

    def test_create_temp_passwd_file_fail_file_does_not_exist_on_failure(self):
        with mock.patch("gnsi_console.open") as mock_open:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_file_handler = mock_open.return_value.__enter__.return_value
                mock_file_handler.writelines = self.raise_ioerror_fh_with_one_arg
                mock_os.path.isfile.return_value = False
                result = self.gnsi_console_module.create_temp_passwd_file(
                    TEST_UPDATED_PASSWORD_FILE_CONTENT.copy())
                assert result[0] == 1
                assert result[1] == (
                    "Failed to create temporary password file with error: " +
                    "IOError in unit test")
                mock_os.path.isfile.assert_called_once_with(
                    PASSWD_FILE_TEMP)

    def test_create_temp_passwd_file_fail_file_removed_on_failure(self):
        with mock.patch("gnsi_console.open") as mock_open:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_file_handler = mock_open.return_value.__enter__.return_value
                mock_file_handler.writelines = self.raise_ioerror_fh_with_one_arg
                mock_os.path.isfile.return_value = True
                result = self.gnsi_console_module.create_temp_passwd_file(TEST_UPDATED_PASSWORD_FILE_CONTENT.copy())
                assert result[0] == 1
                assert result[1] == (
                  "Failed to create temporary password file with error: " +
                  "IOError in unit test")
                mock_os.path.isfile.assert_called_once_with(
                  PASSWD_FILE_TEMP)
                mock_os.remove.assert_called_once_with(PASSWD_FILE_TEMP)

    def test_create_temp_passwd_file_on_failure_file_remove_also_fail(self):
        with mock.patch("gnsi_console.open") as mock_open:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_file_handler = mock_open.return_value.__enter__.return_value
                mock_file_handler.writelines = self.raise_ioerror_fh_with_one_arg
                mock_os.path.isfile.return_value = True
                mock_os.remove = self.raise_exception_os_test
                result = self.gnsi_console_module.create_temp_passwd_file(
                    TEST_UPDATED_PASSWORD_FILE_CONTENT.copy())
                assert result[0] == 1
                assert result[1] == (
                    "Failed to create temporary password file with error: " +
                    "IOError in unit test" +
                    " and also failed to remove temporary file created with error: " +
                    TEST_EXCEPTION_MESSAGE)
                mock_os.path.isfile.assert_called_once_with(
                    PASSWD_FILE_TEMP)

    def test_update_password_file_success(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            result = self.gnsi_console_module.update_password_file(PASSWD_FILE_TEMP)
            assert result[0] == 0
            assert result[1] == "Successfully updated console passwords"
            mock_shutil.move.assert_called_once_with(PASSWD_FILE_TEMP,
                                                     PASSWD_FILE)

    def test_update_password_file_fail_move_failed(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            mock_shutil.move = self.raise_exception_shutil_test
            result = self.gnsi_console_module.update_password_file(PASSWD_FILE_TEMP)
            assert result[0] == 1
            assert result[1] == (
                "Failed to replace original password file "
                "with given password file with error: " +
                TEST_EXCEPTION_MESSAGE)


    def test_update_password_file_fail_file_does_not_exist_on_failure(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_shutil.move = self.raise_exception_shutil_test
                mock_os.path.isfile.return_value = False
                result = self.gnsi_console_module.update_password_file(PASSWD_FILE_TEMP)
                assert result[0] == 1
                assert result[1] == (
                    "Failed to replace original password file with given password file with error: " +
                    TEST_EXCEPTION_MESSAGE)
                mock_os.path.isfile.assert_called_once_with(
                    PASSWD_FILE_TEMP)

    def test_update_password_file_fail_file_removed_on_failure(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_shutil.move = self.raise_exception_shutil_test
                mock_os.path.isfile.return_value = True
                result = self.gnsi_console_module.update_password_file(PASSWD_FILE_TEMP)
                assert result[0] == 1
                assert result[1] == (
                    "Failed to replace original password file with given password file with error: " +
                    TEST_EXCEPTION_MESSAGE)
                mock_os.path.isfile.assert_called_once_with(
                    PASSWD_FILE_TEMP)
                mock_os.remove.assert_called_once_with(PASSWD_FILE_TEMP)

    def test_update_password_file_fail_on_failure_file_remove_also_fail(self):
        with mock.patch("gnsi_console.shutil") as mock_shutil:
            with mock.patch("gnsi_console.os") as mock_os:
                mock_shutil.move = self.raise_exception_shutil_test
                mock_os.path.isfile.return_value = True
                mock_os.remove = self.raise_exception_os_test
                result = self.gnsi_console_module.update_password_file(PASSWD_FILE_TEMP)
                assert result[0] == 1
                assert result[1] == (
                    "Failed to replace original password file with given password file with error: " +
                    TEST_EXCEPTION_MESSAGE +
                    " and also failed to remove given password file with error: " +
                    TEST_EXCEPTION_MESSAGE)
                mock_os.path.isfile.assert_called_once_with(
                    PASSWD_FILE_TEMP)

    def test_set_fail_checkpoint_does_not_exist(self):
        with mock.patch("gnsi_console.os") as mock_os:
            mock_os.path.isfile.return_value = False
            result = self.gnsi_console_module.set([TEST_VALID_PASSWORD_CHANGE_REQEST])
            assert result[0] == 1
            assert result[1] == "Trying to update console password without creating checkpoint"
            mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def test_set_fail_invalid_json(self):
        with mock.patch("gnsi_console.os") as mock_os:
            mock_os.path.isfile.return_value = True
            result = self.gnsi_console_module.set([TEST_INVALID_PASSWORD_CHANGE_REQEST])
            assert result[0] == 1
            assert result[1] == "Failed to parse json formatted password change request: " + TEST_INVALID_PASSWORD_CHANGE_REQEST
            mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def test_set_fail_key_not_present(self):
        with mock.patch("gnsi_console.os") as mock_os:
            mock_os.path.isfile.return_value = True
            result = self.gnsi_console_module.set([TEST_RANDOM_PASSWORD_CHANGE_REQEST])
            assert result[0] == 1
            assert result[1] == "Received invalid password request: %s" % str(json.loads(TEST_RANDOM_PASSWORD_CHANGE_REQEST))
            mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)

    def test_set_read_password_failed(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.read_password_file") as mock_read_password_file:
                mock_os.path.isfile.return_value = True
                mock_read_password_file.return_value = ([], "Read password file failed")
                result = self.gnsi_console_module.set([TEST_VALID_PASSWORD_CHANGE_REQEST])
                assert result[0] == 1
                assert result[1] == "Read password file failed"
                mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                mock_read_password_file.assert_called_once_with()

    def test_set_success(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.read_password_file") as mock_read_password_file:
                with mock.patch("gnsi_console.GnsiConsole.get_hashed_password") as mock_get_hashed_password:
                    with mock.patch("gnsi_console.GnsiConsole.update_password_if_user_found") as mock_update_password_if_user_found:
                        with mock.patch("gnsi_console.GnsiConsole.update_password_file") as mock_update_password_file:
                            with mock.patch("gnsi_console.GnsiConsole.create_temp_passwd_file") as mock_create_temp_passwd_file:
                                mock_os.path.isfile.return_value = True
                                mock_read_password_file.return_value = (TEST_OLD_PASSWORD_FILE_CONTENT.copy(), "")
                                mock_get_hashed_password.return_value = TEST_HASHED_PASSWORD
                                mock_create_temp_passwd_file.return_value = (0, "")
                                mock_update_password_file.return_value = (0, "Successfully updated console passwords")
                                result = self.gnsi_console_module.set([TEST_VALID_PASSWORD_CHANGE_REQEST])
                                assert result[0] == 0
                                assert result[1] == "set: Successfully updated console passwords"
                                assert mock_get_hashed_password.call_count == 2
                                assert mock_update_password_if_user_found.call_count == 2
                                mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                                mock_read_password_file.assert_called_once_with()
                                mock_get_hashed_password.assert_has_calls([mock.call("new_root_text_password"),
                                                                           mock.call("new_second_text_password")])
                                mock_update_password_if_user_found.assert_has_calls([mock.call("root", TEST_HASHED_PASSWORD, TEST_OLD_PASSWORD_FILE_CONTENT),
                                                                           mock.call("second_user", TEST_HASHED_PASSWORD, TEST_OLD_PASSWORD_FILE_CONTENT)])
                                mock_update_password_file.assert_called_once_with(PASSWD_FILE_TEMP)
                                mock_create_temp_passwd_file.assert_called_once_with(TEST_OLD_PASSWORD_FILE_CONTENT)

    def test_set_fail_create_temp_password_file_fail(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.read_password_file") as mock_read_password_file:
                with mock.patch("gnsi_console.GnsiConsole.get_hashed_password") as mock_get_hashed_password:
                    with mock.patch("gnsi_console.GnsiConsole.update_password_if_user_found") as mock_update_password_if_user_found:
                        with mock.patch("gnsi_console.GnsiConsole.create_temp_passwd_file") as mock_create_temp_passwd_file:
                            mock_os.path.isfile.return_value = True
                            mock_read_password_file.return_value = (TEST_OLD_PASSWORD_FILE_CONTENT.copy(), "")
                            mock_get_hashed_password.return_value = TEST_HASHED_PASSWORD
                            mock_create_temp_passwd_file.return_value = (1, "Failed to create temporary password file with error: test error message")
                            result = self.gnsi_console_module.set([TEST_VALID_PASSWORD_CHANGE_REQEST])
                            assert result[0] == 1
                            assert result[1] == "Failed to create temporary password file with error: test error message"
                            assert mock_get_hashed_password.call_count == 2
                            assert mock_update_password_if_user_found.call_count == 2
                            mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                            mock_read_password_file.assert_called_once_with()
                            mock_get_hashed_password.assert_has_calls([mock.call("new_root_text_password"),
                                                                       mock.call("new_second_text_password")])
                            mock_update_password_if_user_found.assert_has_calls([mock.call("root", TEST_HASHED_PASSWORD, TEST_OLD_PASSWORD_FILE_CONTENT),
                                                                       mock.call("second_user", TEST_HASHED_PASSWORD, TEST_OLD_PASSWORD_FILE_CONTENT)])
                            mock_create_temp_passwd_file.assert_called_once_with(TEST_OLD_PASSWORD_FILE_CONTENT)

    def test_set_name_and_password_keys_not_present(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.read_password_file") as mock_read_password_file:
                with mock.patch("gnsi_console.GnsiConsole.get_hashed_password") as mock_get_hashed_password:
                    with mock.patch("gnsi_console.GnsiConsole.update_password_if_user_found") as mock_update_password_if_user_found:
                        with mock.patch("gnsi_console.GnsiConsole.update_password_file") as mock_update_password_file:
                            with mock.patch("gnsi_console.GnsiConsole.create_temp_passwd_file") as mock_create_temp_passwd_file:
                                mock_os.path.isfile.return_value = True
                                mock_read_password_file.return_value = (TEST_OLD_PASSWORD_FILE_CONTENT.copy(), "")
                                mock_get_hashed_password.return_value = TEST_HASHED_PASSWORD
                                mock_create_temp_passwd_file.return_value = (0, "")
                                mock_update_password_file.return_value = (0, "Successfully updated console passwords")
                                remove_name_and_password_keys = json.loads(TEST_VALID_PASSWORD_CHANGE_REQEST)
                                remove_name_and_password_keys["ConsolePasswords"][0].pop("name")
                                remove_name_and_password_keys["ConsolePasswords"][1].pop("password")
                                result = self.gnsi_console_module.set([json.dumps(remove_name_and_password_keys)])
                                assert result[0] == 0
                                assert result[1] == "set: Successfully updated console passwords"
                                mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                                mock_read_password_file.assert_called_once_with()
                                mock_update_password_file.assert_called_once_with(PASSWD_FILE_TEMP)
                                mock_get_hashed_password.assert_not_called()
                                mock_update_password_if_user_found.assert_not_called()

    def test_set_name_hashed_password_fail_for_one_request(self):
        with mock.patch("gnsi_console.os") as mock_os:
            with mock.patch("gnsi_console.GnsiConsole.read_password_file") as mock_read_password_file:
                with mock.patch("gnsi_console.GnsiConsole.get_hashed_password") as mock_get_hashed_password:
                    with mock.patch("gnsi_console.GnsiConsole.update_password_if_user_found") as mock_update_password_if_user_found:
                        with mock.patch("gnsi_console.GnsiConsole.update_password_file") as mock_update_password_file:
                            with mock.patch("gnsi_console.GnsiConsole.create_temp_passwd_file") as mock_create_temp_passwd_file:
                                mock_os.path.isfile.return_value = True
                                mock_read_password_file.return_value = (TEST_OLD_PASSWORD_FILE_CONTENT.copy(), "")
                                mock_get_hashed_password.side_effect = [TEST_HASHED_PASSWORD, ""]
                                mock_create_temp_passwd_file.return_value = (0, "")
                                mock_update_password_file.return_value = (0, "Successfully updated console passwords")
                                result = self.gnsi_console_module.set([TEST_VALID_PASSWORD_CHANGE_REQEST])
                                assert result[0] == 0
                                assert result[1] == "set: Successfully updated console passwords"
                                assert mock_get_hashed_password.call_count == 2
                                mock_os.path.isfile.assert_called_once_with(PASSWD_FILE_CHECKPOINT_FILE)
                                mock_read_password_file.assert_called_once_with()
                                mock_get_hashed_password.assert_has_calls([mock.call("new_root_text_password"),
                                                                           mock.call("new_second_text_password")])
                                mock_update_password_if_user_found.assert_called_once_with("root", TEST_HASHED_PASSWORD, TEST_OLD_PASSWORD_FILE_CONTENT)
                                mock_update_password_file.assert_called_once_with(PASSWD_FILE_TEMP)
                                mock_create_temp_passwd_file.assert_called_once_with(TEST_OLD_PASSWORD_FILE_CONTENT)

    def test_register(self):
        result = register()
        assert result[0] == GnsiConsole
        assert result[1] == MOD_NAME

    @classmethod
    def teardown_class(cls):
        print("TEARDOWN")
