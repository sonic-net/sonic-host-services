"""File stat handler"""

from host_modules import host_service
import paramiko
import requests
import scp
import stat

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

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='ssssss', out_signature='is')
    def download(self, hostname, username, password, remote_path, local_path, protocol):
        """
        Download a file from a remote server using various protocols.

        Args:
            hostname (str): The hostname or IP address of the remote server.
            username (str): The username for authentication.
            password (str): The password for authentication.
            remote_path (str): The path to the file on the remote server or URL.
            local_path (str): The path to save the file locally.
            protocol (str): The protocol to use ("SFTP", "HTTP", "HTTPS", "SCP").

        Returns:
            tuple: (int, str) - 0 and an empty string on success, 1 and an error message on failure.
        """
        try:
            # 1. Do not override any file
            if os.path.exists(local_path):
                return EXIT_FAILURE, f"File already exists: {local_path}"

            # 2. The directory we are writing to must be world writable
            dir_path = os.path.dirname(local_path) or "."
            try:
                dir_stat = os.stat(dir_path)
            except Exception as e:
                return EXIT_FAILURE, f"Directory not found: {dir_path} ({e})"
            if not (dir_stat.st_mode & stat.S_IWOTH):
                return EXIT_FAILURE, f"Directory is not world writable: {dir_path}"

            protocol = protocol.upper()  # Normalize protocol string to uppercase

            if protocol == "SFTP":
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    ssh.connect(hostname, username=username, password=password)
                    sftp = ssh.open_sftp()
                    sftp.get(remote_path, local_path)
                    sftp.close()
                    ssh.close()
                    return 0, ""
                except Exception as e:
                    ssh.close()
                    return EXIT_FAILURE, str(e)

            elif protocol in ["HTTP", "HTTPS"]:
                response = requests.get(remote_path, auth=(username, password), stream=True)
                response.raise_for_status()
                with open(local_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

            elif protocol == "SCP":
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    ssh.connect(hostname, username=username, password=password)
                    scp_client = scp.SCPClient(ssh.get_transport())
                    scp_client.get(remote_path, local_path)
                    scp_client.close()
                    ssh.close()
                    return 0, ""
                except Exception as e:
                    ssh.close()
                    return 1, str(e)

            else:
                return EXIT_FAILURE, f"Unsupported protocol: {protocol}"

            return 0, ""  # Success

        except Exception as e:
            return EXIT_FAILURE, str(e)

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def remove(self, path):
        """
        Remove a file at the specified path.

        Args:
            path (str): The path to the file to remove.

        Returns:
            tuple: (int, str) - 0 and an empty string on success, 1 and an error message on failure.
        """
        try:
            # Check if file exists
            if not os.path.exists(path):
                return EXIT_FAILURE, f"File not found: {path}"

            # Check if file is world-writable (deletable by world)
            file_stat = os.stat(path)
            if not (file_stat.st_mode & stat.S_IWOTH):
                return EXIT_FAILURE, f"File is not world writable (deletable by world): {path}"

            os.remove(path)
            return 0, ""
        except Exception as e:
            return EXIT_FAILURE, str(e)

