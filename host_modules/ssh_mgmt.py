"""SSH Management.

This host service module implements the backend support for gNSI ssh rotation.
"""

import json
import logging
import os
import shutil

from host_modules import host_service


MOD_NAME = 'ssh_mgmt'
CHECKPOINT_DIR = '/tmp/ssh_checkpoint'
COPY_TEMP_FILE = 'ssh_mgmt_file_temp'
CA_PUB_KEY_NAME = 'ssh_ca_pub_key'
CA_PUB_KEY_TEMP = 'ssh_ca_pub_key_temp'

CA_PUB_KEY_DIR = '/etc/sonic/ssh'
PERSISTENT_CA_PUB_KEY_DIRS = [
    CA_PUB_KEY_DIR
]

ROOT_AUTHORIZED_KEYS_NAME = 'authorized_keys'
ROOT_AUTHORIZED_KEYS_TEMP = 'authorized_keys_temp'

ROOT_AUTHORIZED_KEYS_DIR = '/etc/sonic/ssh/root'
PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS = [
    ROOT_AUTHORIZED_KEYS_DIR
]

ROOT_AUTHORIZED_USERS_NAME = 'authorized_users'
ROOT_AUTHORIZED_USERS_TEMP = 'authorized_users_temp'

ROOT_AUTHORIZED_USERS_DIR = '/etc/sonic/ssh/root'
PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS = [
    ROOT_AUTHORIZED_USERS_DIR
]

logger = logging.getLogger(__name__)


class SshMgmt(host_service.HostModule):
    """DBus endpoint that updates ssh related files."""

    @staticmethod
    def _write_options(f, options):
        first = True
        for option in options:
            if 'name' not in option:
                continue
            if first:
                first = False
            else:
                f.write(',')
            if option.get('value'):
                f.write(option['name'] + '="' + option['value'] + '"')
            else:
                f.write(option['name'])
        if not first:
            f.write(' ')

    @staticmethod
    def _copy_file(src, dest):
        """This method first copies the source file to a temp file in the
        destination directory. Then moves the temp file to the destination file.
        If the source file does not exist, it will return success, so that we
        can support ssh_mgmt update even if the files are missing.
        """
        ret_code = 0
        ret_msg = 'Successfully copy file from %s to %s' % (src, dest)
        if not os.path.exists(src):
            logger.error('Source file %s does not exist in ssh_mgmt copy.', src)
            return ret_code, ret_msg
        try:
            dir = os.path.dirname(dest)
            os.makedirs(dir, exist_ok=True)
            shutil.copyfile(src, os.path.join(dir, COPY_TEMP_FILE))
            shutil.move(os.path.join(dir, COPY_TEMP_FILE), dest)
        except Exception:
            ret_code = 1
            ret_msg = 'Failed to copy file from %s to %s' % (src, dest)
            logger.error('%s: %s\n', MOD_NAME, ret_msg)
        try:
            os.remove(os.path.join(dir, COPY_TEMP_FILE))
        except Exception:
            pass
        return ret_code, ret_msg

    @staticmethod
    def _copy_files(src, dest):
        """This method is the same as _copy_file, but copies an array of files.
        The length of the src array and the dest array must be the same.
        Files will be copied from src to dest with the same index.
        """
        if len(src) != len(dest):
            return 1, 'Length of src and dest do not match in _copy_files'
        ret_code = 0
        ret_msg = ''
        for i in range(len(src)):
            code, msg = SshMgmt._copy_file(src[i], dest[i])
            ret_code |= code
            if ret_msg:
                ret_msg += ' & '
            ret_msg += msg
        return ret_code, ret_msg

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def create_checkpoint(self, options):
        if os.path.isdir(CHECKPOINT_DIR):
            logger.error('%s: ssh_mgmt.create_checkpoint is called while'
                         'checkpoint still exists', MOD_NAME)
            try:
                shutil.rmtree(CHECKPOINT_DIR)
            except Exception:
                logger.error('%s: Failed to delete old ssh mgmt checkpoint %s '
                             'in ssh_mgmt.create_checkpoint!\n', MOD_NAME,
                             CHECKPOINT_DIR)
                return 1, 'Error in deleting checkpoint'

        os.makedirs(CHECKPOINT_DIR, exist_ok=True)

        code, msg = SshMgmt._copy_files([
            os.path.join(CA_PUB_KEY_DIR, CA_PUB_KEY_NAME),
            os.path.join(ROOT_AUTHORIZED_KEYS_DIR, ROOT_AUTHORIZED_KEYS_NAME),
            os.path.join(ROOT_AUTHORIZED_USERS_DIR, ROOT_AUTHORIZED_USERS_NAME)
        ], [
            os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_NAME),
            os.path.join(CHECKPOINT_DIR, ROOT_AUTHORIZED_KEYS_NAME),
            os.path.join(CHECKPOINT_DIR, ROOT_AUTHORIZED_USERS_NAME)
        ])
        if code != 0:
            logger.error('%s: Failed to create ssh mgmt checkpoint!\n',
                         MOD_NAME)
            try:
                shutil.rmtree(CHECKPOINT_DIR)
            except Exception:
                logger.error('%s: Failed to delete ssh mgmt checkpoint %s! This'
                             ' might block gNSI ssh operations!\n', MOD_NAME,
                             CHECKPOINT_DIR)
        else:
            msg = 'Successfully created checkpoint'
        return code, msg

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def restore_checkpoint(self, options):
        if not os.path.isdir(CHECKPOINT_DIR):
            return 1, 'Checkpoint does not exist'

        # We will restore the checkpoint to the persistent locations as well.
        src_files = ([os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_NAME)] * (
            len(PERSISTENT_CA_PUB_KEY_DIRS)+1))+(
                [os.path.join(CHECKPOINT_DIR, ROOT_AUTHORIZED_KEYS_NAME)] * (
                    len(PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS)+1))+(
                        [os.path.join(
                            CHECKPOINT_DIR, ROOT_AUTHORIZED_USERS_NAME)] * (
                                len(PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS)+1))

        dest_files = [os.path.join(x, CA_PUB_KEY_NAME)
                      for x in (PERSISTENT_CA_PUB_KEY_DIRS + [CA_PUB_KEY_DIR])
                      ] + [os.path.join(x, ROOT_AUTHORIZED_KEYS_NAME)
                           for x in (PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS+[
                               ROOT_AUTHORIZED_USERS_DIR])]+[
            os.path.join(x, ROOT_AUTHORIZED_USERS_NAME)
            for x in (PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS+[
                ROOT_AUTHORIZED_USERS_DIR])]

        code, msg = SshMgmt._copy_files(src_files, dest_files)
        if code != 0:
            logger.error('%s: Failed to restore ssh mgmt checkpoint!\n',
                         MOD_NAME)

        try:
            shutil.rmtree(CHECKPOINT_DIR)
        except Exception:
            logger.error('%s: Failed to delete ssh mgmt checkpoint %s!\n',
                         MOD_NAME, CHECKPOINT_DIR)
            code = 1
            if msg:
                msg += ' & '
            msg += 'Error in deleting checkpoint'

        if code == 0:
            msg = 'Successfully restored checkpoint'
        return code, msg

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def delete_checkpoint(self, options):
        if not os.path.isdir(CHECKPOINT_DIR):
            return 1, 'Checkpoint does not exist'

        try:
            shutil.rmtree(CHECKPOINT_DIR)
        except Exception:
            logger.error('%s: Failed to delete ssh mgmt checkpoint %s!\n',
                         MOD_NAME, CHECKPOINT_DIR)
            return 1, 'Error in deleting checkpoint'
        return 0, 'Successfully deleted checkpoint'

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature='as', out_signature='is')
    def set(self, options):
        if not os.path.isdir(CHECKPOINT_DIR):
            return 1, 'Update ssh config before creating checkpoint'

        try:
            json_content = json.loads(options[0])
        except json.JSONDecodeError:
            return 1, 'Invalid JSON'

        if len(json_content) == 0:
            logger.error('%s: Empty request in ssh_mgmt.set.\n', MOD_NAME)

        code = 0
        for ssh_key in json_content:
            if ssh_key == 'SshCaPublicKey':
                # Write the content to a temp file.
                with open(os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_TEMP), 'w') as f:
                    for key in json_content[ssh_key]:
                        f.write(key + '\n')
                # Copy the temp file.
                code, msg = SshMgmt._copy_files(
                    [os.path.join(CHECKPOINT_DIR, CA_PUB_KEY_TEMP)] *
                    (len(PERSISTENT_CA_PUB_KEY_DIRS)+1),
                    [os.path.join(CA_PUB_KEY_DIR, CA_PUB_KEY_NAME)]+[
                        os.path.join(x, CA_PUB_KEY_NAME)
                        for x in PERSISTENT_CA_PUB_KEY_DIRS]
                )

            elif ssh_key == 'SshAccountKeys':
                # Write the content to a temp file.
                with open(
                        os.path.join(CHECKPOINT_DIR,
                                     ROOT_AUTHORIZED_KEYS_TEMP),
                        'w') as f:
                    for account in json_content[ssh_key]:
                        if account.get('account') != 'root':
                            continue
                        if 'keys' not in account:
                            continue
                        for key in account['keys']:
                            if not key.get('key'):
                                continue
                            if 'options' in key:
                                SshMgmt._write_options(f, key['options'])
                            f.write(key['key'] + '\n')
                # Copy the temp file.
                code, msg = SshMgmt._copy_files(
                    [os.path.join(CHECKPOINT_DIR, ROOT_AUTHORIZED_KEYS_TEMP)] *
                    (len(PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS)+1),
                    [os.path.join(ROOT_AUTHORIZED_KEYS_DIR,
                                  ROOT_AUTHORIZED_KEYS_NAME)]+[os.path.join(
                                      x, ROOT_AUTHORIZED_KEYS_NAME)
                        for x in PERSISTENT_ROOT_AUTHORIZED_KEYS_DIRS]
                )

            elif ssh_key == 'SshAccountUsers':
                # Write the content to a temp file.
                with open(
                        os.path.join(CHECKPOINT_DIR,
                                     ROOT_AUTHORIZED_USERS_TEMP),
                        'w') as f:
                    for account in json_content[ssh_key]:
                        if account.get('account') != 'root':
                            continue
                        if 'users' not in account:
                            continue
                        for user in account['users']:
                            if not user.get('name'):
                                continue
                            if 'options' in user:
                                SshMgmt._write_options(f, user['options'])
                            f.write(user['name'] + '\n')
                # Copy the temp file.
                code, msg = SshMgmt._copy_files(
                    [os.path.join(CHECKPOINT_DIR, ROOT_AUTHORIZED_USERS_TEMP)] *
                    (len(PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS)+1),
                    [os.path.join(ROOT_AUTHORIZED_USERS_DIR,
                                  ROOT_AUTHORIZED_USERS_NAME)]+[os.path.join(
                                      x, ROOT_AUTHORIZED_USERS_NAME)
                        for x in PERSISTENT_ROOT_AUTHORIZED_USERS_DIRS]
                )

            else:
                logger.error('%s: Invalid key in ssh_mgmt.set: %s.\n', MOD_NAME,
                             ssh_key)

        if code == 0:
            msg = 'Successfully set credentials'
        return code, msg


def register():
    """Return class name."""
    return SshMgmt, MOD_NAME
