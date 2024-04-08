"""Systemd service handler"""

from host_modules import host_service
import subprocess

MOD_NAME = 'systemd'
ALLOWED_SERVICES = ['snmp', 'swss', 'dhcp_relay', 'radv', 'restapi', 'lldp', 'sshd']
EXIT_FAILURE = 1


class SystemdService(host_service.HostModule):
    """
    DBus endpoint that executes the service command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def restart_service(self, service):
        if service not in ALLOWED_SERVICES:
            return EXIT_FAILURE, "Dbus does not support {} service management".format(service)

        cmd = ['/usr/bin/systemctl', 'reset-failed', service]
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            msg = result.stderr.decode()
            return result.returncode, msg

        cmd = ['/usr/bin/systemctl', 'restart', service]
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            msg = result.stderr.decode()
        
        return result.returncode, msg
    
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def stop_service(self, service):
        if service not in ALLOWED_SERVICES:
            return EXIT_FAILURE, "Dbus does not support {} service management".format(service)

        cmd = ['/usr/bin/systemctl', 'stop', service]
        result = subprocess.run(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            msg = result.stderr.decode()
        return result.returncode, msg
