"""File stat handler"""

from host_modules import host_service
import paramiko
import requests
import scp
import stat

from urllib.parse import urlparse

MOD_NAME = 'file'
EXIT_FAILURE = 1

# Protocol name -> the single URL scheme that protocol is allowed to use.
HTTP_PROTOCOL_SCHEMES = {
    "HTTP": "http",
    "HTTPS": "https",
}

# (connect, read) timeouts in seconds, so a download cannot hang the daemon forever.
HTTP_TIMEOUT = (10, 60)

import os


def normalize_host(host):
    """
    Normalize a host so that equivalent spellings compare equal.

    Lowercases, strips IPv6 brackets, and drops the root label, so that
    "EXAMPLE.COM.", "example.com" and "[::1]"/"::1" are treated as written.
    """
    if not host:
        return ""
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.endswith("."):
        host = host[:-1]
    return host


def validate_http_url(remote_path, hostname, protocol):
    """
    Check that remote_path is an HTTP(S) URL addressed to hostname.

    The HTTP(S) branch of download() fetches remote_path directly, so without this
    check the caller-supplied hostname is ignored and remote_path alone decides which
    host the daemon contacts. That lets a caller aim the daemon at hosts it can reach
    but the caller cannot, and hands the supplied credentials to whatever host the URL
    names. Requiring the URL host to match hostname restores the same relationship the
    SFTP and SCP branches already have, where hostname is the peer being contacted.

    Args:
        remote_path (str): The URL supplied by the caller.
        hostname (str): The host the caller declared it wanted to reach.
        protocol (str): Normalized protocol name, "HTTP" or "HTTPS".

    Returns:
        str: An error message, or None when remote_path is acceptable.
    """
    if not remote_path or not isinstance(remote_path, str):
        return "A URL must be supplied as remote_path for {} downloads".format(protocol)

    expected_scheme = HTTP_PROTOCOL_SCHEMES.get(protocol)
    if expected_scheme is None:
        return "Unsupported protocol: {}".format(protocol)

    try:
        url = urlparse(remote_path)
    except ValueError as e:
        return "Malformed URL in remote_path: {}".format(e)

    if url.scheme.lower() != expected_scheme:
        return (
            "URL scheme '{}' does not match protocol {}; expected a {}:// URL".format(
                url.scheme, protocol, expected_scheme
            )
        )

    try:
        url_host = url.hostname
    except ValueError as e:
        return "Malformed host in remote_path: {}".format(e)

    if not url_host:
        return "URL in remote_path has no host: {}".format(remote_path)

    # Credentials belong in the username/password arguments, not in the URL. A URL of
    # the form http://trusted@attacker/ reads as "trusted" but resolves to "attacker".
    if url.username is not None or url.password is not None:
        return "URL in remote_path must not contain embedded credentials"

    declared_host = normalize_host(hostname)
    if not declared_host:
        return "A hostname must be supplied for {} downloads".format(protocol)

    if normalize_host(url_host) != declared_host:
        return (
            "URL host '{}' does not match the requested hostname '{}'".format(
                url_host, hostname
            )
        )

    return None

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
                    return 1, str(e)

            elif protocol in ["HTTP", "HTTPS"]:
                error = validate_http_url(remote_path, hostname, protocol)
                if error:
                    return EXIT_FAILURE, error

                # Only send credentials when the caller actually supplied them.
                auth = (username, password) if username else None

                # Redirects are not followed: a permitted host could otherwise bounce
                # the request to one the caller is not allowed to name directly.
                response = requests.get(
                    remote_path,
                    auth=auth,
                    stream=True,
                    timeout=HTTP_TIMEOUT,
                    allow_redirects=False,
                )
                if response.is_redirect or response.is_permanent_redirect:
                    return EXIT_FAILURE, (
                        "Refusing to follow redirect from {} to {}".format(
                            remote_path, response.headers.get("Location", "an unknown location")
                        )
                    )
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

            # Check if parent directory is world-writable (deletable by world)
            dir_path = os.path.dirname(path) or "."
            try:
                dir_stat = os.stat(dir_path)
            except Exception as e:
                return EXIT_FAILURE, f"Directory not found: {dir_path} ({e})"
            if not (dir_stat.st_mode & stat.S_IWOTH):
                return EXIT_FAILURE, f"Directory is not world writable: {dir_path}"

            os.remove(path)
            return 0, ""
        except Exception as e:
            return EXIT_FAILURE, str(e)

