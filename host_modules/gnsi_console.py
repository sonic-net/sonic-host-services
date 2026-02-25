"""gNSI console module used to manage console credentials"""

import json
import os
import shutil
import logging

from host_modules import host_service
from utils.run_cmd import _run_command

MOD_NAME = 'gnsi_console'

# File path which consists of console password
PASSWD_FILE = "/etc/shadow"
PASSWD_FILE_CHECKPOINT_FILE = PASSWD_FILE + "_checkpoint"
PASSWD_FILE_TEMP = PASSWD_FILE + "_temp"

# Openssl command to generate hashed password using SHA512-based algorithm
OPENSSL_COMMAND = "openssl passwd -6 "

# Constant trailing info regarding each password in the password file
TRAILING_PASSWORD_INFO = ":12215:0:99999:7:::\n"

logger = logging.getLogger(__name__)

class GnsiConsole(host_service.HostModule):
    """DBus endpoint used to update console credentials for an existing user
    """

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def create_checkpoint(self, options):
        """Creates checkpoint for console password file so that the current
           state can be restored later using restore_checkpoint(). create_checkpoint() will be
           invoked when gNSI client starts the password change process."""
        try:
            shutil.copy(PASSWD_FILE, PASSWD_FILE_CHECKPOINT_FILE)
        except Exception as error:
            return 1, "Failed to create checkpoint with error: " + str(error)
        return 0, "Successfully created checkpoint"

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def restore_checkpoint(self, options):
        """Restore the state of the console password file to the state when
           create_checkpoint() is called, i.e., to the state when the password change process has started.
           Here, a move operation is performed as move is an atomic operation."""
        if not os.path.isfile(PASSWD_FILE_CHECKPOINT_FILE):
            return 1, "Checkpoint file is not present"

        # Update the /etc/shadow with the checkpoint file
        result = self.update_password_file(PASSWD_FILE_CHECKPOINT_FILE)
        return result[0], "restore_checkpoint: " + result[1]

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def delete_checkpoint(self, options):
        """Deletes the checkpoint file created in create_checkpoint().
           delete_checkpoint() is invoked at the end of the successful password
           change process."""
        try:
            os.remove(PASSWD_FILE_CHECKPOINT_FILE)
        except Exception as error:
            return 1, "Failed to delete checkpoint with error: " + str(error)
        return 0, "Successfully deleted checkpoint"

    def get_hashed_password(self, text_password):
        """Generates and returns hashed password for given text password using
           SHA-512-based password algorithm. Returns empty string on failure."""
        rc, stdout, stderr = _run_command(OPENSSL_COMMAND + text_password)
        if rc:
            logger.error("%s: Failed to get hash for given text password "
                             "with stdout: %s, stderr: %s"
                             % (MOD_NAME, stdout, stderr))
            return ""
        return stdout[0]

    def read_password_file(self):
        """Read contents of /etc/shadow password file and return its contents
           in the form of a list where each line is an element in the list"""
        try:
            with open(PASSWD_FILE, 'r') as f:
                password_file_content_list = f.readlines()
        except IOError as error:
            return [], "Failed to read password file with error: " + str(error)
        return password_file_content_list, ""

    def update_password_if_user_found(self, user_name, user_password,
                                      password_file_content_list):
        """If user with user_name is found in password_file_content_list, then
           this function will update password with user_password in
           password_file_content_list. Logs an error if user_name is not found"""
        found_user = False
        for index,each_line in enumerate(password_file_content_list):
            if each_line.startswith(user_name):
                found_user = True
                password_file_content_list[index] = (user_name + ":" +
                                                     user_password +
                                                     TRAILING_PASSWORD_INFO)
        if not found_user:
            logger.error("%s: The given user name: %s does not exist in the "
                             "password file" % (MOD_NAME, user_name))

    def create_temp_passwd_file(self, password_file_content_list):
        """Writes the contents of password_file_content_list into a temporary
           file"""
        rc = 0
        output = ""
        try:
            with open(PASSWD_FILE_TEMP, 'w') as f:
                f.writelines(password_file_content_list)
        except IOError as error:
            rc = 1
            output = ("Failed to create temporary password file with error: "
                      + str(error))

        # Remove temporary password file if it exists after failing to create
        # this file with password_file_content_list
        if rc and os.path.isfile(PASSWD_FILE_TEMP):
            try:
                os.remove(PASSWD_FILE_TEMP)
            except Exception as error:
                output += (" and also failed to remove temporary file "
                           "created with error: " + str(error))
        return rc, output

    def update_password_file(self, given_password_file):
        """Overwrites /etc/shadow with given_password_file through a move operation """
        rc = 0
        output = "Successfully updated console passwords"
        try:
            shutil.move(given_password_file, PASSWD_FILE)
        except Exception as error:
            rc = 1
            output = ("Failed to replace original password file with "
                      "given password file with error: "
                      + str(error))

        # Remove given_password_file if it exists after failing to overwrite
        # /etc/shadow with given_password_file
        if rc and os.path.isfile(given_password_file):
            try:
                os.remove(given_password_file)
            except Exception as error:
                output += (" and also failed to remove given password file "
                           "with error: " + str(error))

        return rc, output

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def set(self, options):
        """Updates console passwords for exisitng users based on input request.
           This API does not support creation or deletion of new user accounts."""
        if not os.path.isfile(PASSWD_FILE_CHECKPOINT_FILE):
            return 1, "Trying to update console password without creating checkpoint"

        """Convert input json formatted password set request into python dict.
           console_password_info_dict is a python dict with the following format:
              {
               "ConsolePasswords": [
                 { "name": "alice", "password" : "password-alice" },
                 { "name": "bob", "password" : "password-bob" }
               ]
              }
        """
        try:
            console_password_info_dict = json.loads(options[0])
        except json.JSONDecodeError:
            return 1, ("Failed to parse json formatted password change request: "
                       + options[0])

        if "ConsolePasswords" not in console_password_info_dict:
            return 1, "Received invalid password request: %s" % str(console_password_info_dict)

        # Return on failed to read contents of /etc/shadow file
        password_file_content_list, errstr = self.read_password_file()
        if not password_file_content_list:
            return 1, errstr

        # Iterate over each line in password file and update the passwords for
        # the corresponding users in the input request
        for index, each_request in enumerate(console_password_info_dict["ConsolePasswords"]):
            # Skip processing the current element in input request if
            # either "name" or "password" key is missing
            if "name" not in each_request or "password" not in each_request:
                logger.error("%s: Either name or password is not present at "
                                 "index %d in password change request: %s"
                                 % (MOD_NAME, index, str(console_password_info_dict)))
                continue

            hashed_password = self.get_hashed_password(each_request["password"])
            if not hashed_password:
                continue

            self.update_password_if_user_found(each_request["name"], hashed_password,
                                               password_file_content_list)

        # Create a temporary password file with new changes
        err, errstr = self.create_temp_passwd_file(password_file_content_list)
        if err:
            return err, errstr

        # Update the contents in /etc/shadow password file
        result = self.update_password_file(PASSWD_FILE_TEMP)
        return result[0], "set: " + result[1]


def register():
    """Return the class name"""
    return GnsiConsole, MOD_NAME
