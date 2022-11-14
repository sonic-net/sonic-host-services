"""Config command handler"""

from host_modules import host_service
import subprocess
import os

MOD_NAME = 'config'
DEFAULT_CONFIG = '/etc/sonic/config_db.json'

class Config(host_service.HostModule):
    """
    DBus endpoint that executes the config command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def reload(self, config_db_json):

        cmd = ['/usr/local/bin/config', 'reload', '-y']
        config_db_json = config_db_json.strip()
        if config_db_json and len(config_db_json):
            config_file = '/tmp/config_db.json'
            try:
                with open(config_file, 'w') as fp:
                    fp.write(config_db_json)
            except Exception as err:
                return -1, "Fail to create config file: %s"%str(err)
            cmd.append(config_file)

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines:
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
            for line in lines:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg

def register():
    """Return class and module name"""
    return Config, MOD_NAME

