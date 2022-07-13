"""Config command handler"""

from host_modules import host_service
import subprocess
import os
import shutil

MOD_NAME = 'config'
DEFAULT_CONFIG = '/etc/sonic/config_db.json'

class Config(host_service.HostModule):
    """
    DBus endpoint that executes the config command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def reload(self, config_file):

        cmd = ['/usr/local/bin/config', 'reload', '-y']
        if config_file and config_file != DEFAULT_CONFIG:
            if not os.path.exists(config_file):
                return -1, "Can't find %s"%config_file
            # Persistent Config
            shutil.move(config_file, DEFAULT_CONFIG)

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
    def save(self, config_file):

        cmd = ['/usr/local/bin/config', 'save', '-y']
        if config_file and config_file != DEFAULT_CONFIG:
            cmd.append(config_file)

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
    return Config, MOD_NAME

