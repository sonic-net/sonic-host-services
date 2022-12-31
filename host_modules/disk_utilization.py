"""Disk utilization "df -h" command output handler"""

from host_modules import host_service
import subprocess

MOD_NAME = 'diskutil'

class DiskUtilization(host_service.HostModule):
    """
    DBus endpoint that executes the "df -h" command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='', out_signature='ia{ss}')
    def get_disk_util(self):
        cmd = ['/usr/bin/df', '-h']

        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = result.stdout.decode().split('\n')
        headers = lines[0].split()
        rows = []
        for line in lines[1:]:
            fields = line.split()
            if not fields:
                continue
            row = {}
            for i, field in enumerate(fields):
                row[headers[i]] = field
            rows.append(row)
        return result.returncode, rows

def register():
    """Return class and module name"""
    return DiskUtilization, MOD_NAME

