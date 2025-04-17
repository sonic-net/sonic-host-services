"""File stat handler"""

from host_modules import host_service
import subprocess
import paramiko

MOD_NAME = 'file'
EXIT_FAILURE = 1

import os

class FileService(host_service.HostModule):
    """
    Dbus endpoint that executes the file command
    """
    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='ia{ss}')
    def get_file_stat(self, path):
        if not path:
            return EXIT_FAILURE, {'error': 'Dbus get_file_stat called with no path specified'}

        try:
            file_stat = os.stat(path)

            # Get last modified time in nanoseconds since epoch
            last_modified = int(file_stat.st_mtime * 1e9)  # Convert seconds to nanoseconds

            # Get permissions in octal format
            permissions = oct(file_stat.st_mode)[-3:]

            # Get file size in bytes
            size = file_stat.st_size

            # Get current umask
            current_umask = os.umask(0)
            os.umask(current_umask)  # Reset umask to previous value

            return 0, {
                'path': path,
                'last_modified': str(last_modified),  # Converting to string to maintain consistency
                'permissions': permissions,
                'size': str(size),  # Converting to string to maintain consistency
                'umask': oct(current_umask)[-3:]
            }

        except Exception as e:
            return EXIT_FAILURE, {'error': str(e)}

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='sssss', out_signature='i')
    def download(self, hostname, username, password, remote_path, local_path):
        """
        Download a file from a remote server using SSH.

        Args:
            hostname (str): The hostname or IP address of the remote server.
            username (str): The username for SSH authentication.
            password (str): The password for SSH authentication.
            remote_path (str): The path to the file on the remote server.
            local_path (str): The path to save the file locally.

        Returns:
            int: 0 on success, 1 on failure.
        """
        ssh = paramiko.SSHClient()
        try:
            # Create an SSH client
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # Connect to the remote server
            ssh.connect(hostname, username=username, password=password)

            # Use SFTP to download the file
            sftp = ssh.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()

            return 0  # Success
        except Exception as e:
            return EXIT_FAILURE, {'error': str(e)}
        finally:
            ssh.close()