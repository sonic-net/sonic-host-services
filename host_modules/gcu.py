"""Generic config updater command handler"""

from host_modules import host_service
import subprocess

MOD_NAME = 'gcu'

class GCU(host_service.HostModule):
    """
    DBus endpoint that executes the generic config updater command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def apply_patch_db(self, patch_file_path):

        cmd = ['/usr/local/bin/config', 'apply-patch', '-f', 'CONFIGDB', patch_file_path]

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines[::-1]:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def apply_patch_yang(self, patch_file_path):

        cmd = ['/usr/local/bin/config', 'apply-patch', '-f', 'SONICYANG', patch_file_path]

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines[::-1]:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def create_checkpoint(self, checkpoint_file):

        cmd = ['/usr/local/bin/config', 'checkpoint', checkpoint_file]

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines[::-1]:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def delete_checkpoint(self, checkpoint_file):

        cmd = ['/usr/local/bin/config', 'delete-checkpoint', checkpoint_file]

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines[::-1]:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg

def register():
    """Return the class name"""
    return GCU, MOD_NAME

