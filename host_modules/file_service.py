"""File stat handler"""

import errno
import os
import secrets
import stat
from urllib.parse import urlparse

from host_modules import host_service
import paramiko
import requests
import scp

MOD_NAME = 'file'
EXIT_FAILURE = 1

# Protocol name -> the single URL scheme that protocol is allowed to use.
HTTP_PROTOCOL_SCHEMES = {
    "HTTP": "http",
    "HTTPS": "https",
}

# Connect and per-read inactivity timeouts in seconds; this is not a total deadline.
HTTP_TIMEOUT = (10, 60)

# Used only when the destination filesystem does not support O_TMPFILE.
HTTP_TEMP_PREFIX = ".sonic-file-download-"
HTTP_TEMP_ATTEMPTS = 100

# Trusted OpenSSH host-key stores for SFTP and SCP downloads.
SSH_KNOWN_HOSTS_FILES = (
    "/etc/ssh/ssh_known_hosts",
    "/root/.ssh/known_hosts",
)


def _same_file(left, right):
    """Return whether two stat results identify the same filesystem object."""
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _create_http_temp_file(dir_fd):
    """
    Create a temporary download inode in the destination directory.

    Prefer O_TMPFILE so a failed download never has a pathname that another
    process can replace. The named fallback is published from its open file
    descriptor, not from its temporary pathname.
    """
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    tmpfile_flag = getattr(os, "O_TMPFILE", 0)
    if tmpfile_flag:
        try:
            return os.open(".", flags | tmpfile_flag, 0o666, dir_fd=dir_fd), None
        except OSError as e:
            if e.errno not in (errno.EINVAL, errno.EISDIR, errno.EOPNOTSUPP):
                raise

    flags |= os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(HTTP_TEMP_ATTEMPTS):
        temp_name = "{}{}.tmp".format(HTTP_TEMP_PREFIX, secrets.token_hex(16))
        try:
            return os.open(temp_name, flags, 0o666, dir_fd=dir_fd), temp_name
        except FileExistsError:
            continue

    raise FileExistsError(
        errno.EEXIST,
        "Unable to create a unique temporary download file",
    )


def _remove_named_temp_file(dir_fd, temp_name, temp_stat):
    """Remove the named fallback only while it still names our open inode."""
    try:
        current_stat = os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return

    if not _same_file(current_stat, temp_stat):
        return

    try:
        os.unlink(temp_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _write_http_response(response, local_path, dir_path, dir_fd):
    """
    Stream a response to a private inode and publish it without overwriting.

    The final pathname is created only after the complete response is written.
    Linking from /proc/self/fd keeps publication bound to the inode we opened,
    even if a named fallback is renamed or replaced while streaming.
    """
    destination_name = os.path.basename(local_path)
    if not destination_name:
        raise ValueError("Destination path has no file name: {}".format(local_path))

    temp_fd, temp_name = _create_http_temp_file(dir_fd)
    temp_stat = os.fstat(temp_fd)
    try:
        # Keep temp_fd open for descriptor-based publication.
        with os.fdopen(os.dup(temp_fd), "wb") as output:
            for chunk in response.iter_content(chunk_size=8192):
                output.write(chunk)

        try:
            current_dir_stat = os.stat(dir_path)
        except OSError as e:
            raise OSError(
                "Destination directory changed during download: {} ({})".format(
                    dir_path, e
                )
            )

        if not _same_file(os.fstat(dir_fd), current_dir_stat):
            raise OSError(
                "Destination directory changed during download: {}".format(dir_path)
            )

        try:
            os.link(
                "/proc/self/fd/{}".format(temp_fd),
                destination_name,
                dst_dir_fd=dir_fd,
                follow_symlinks=True,
            )
        except FileExistsError:
            raise FileExistsError(
                errno.EEXIST,
                "File already exists: {}".format(local_path),
                local_path,
            )
    finally:
        try:
            if temp_name is not None:
                _remove_named_temp_file(dir_fd, temp_name, temp_stat)
        finally:
            os.close(temp_fd)


def create_http_session():
    """Create an HTTP session isolated from process environment credentials."""
    session = requests.Session()
    # This root daemon must not inherit proxy settings or credentials from .netrc.
    # Authentication and the destination are explicit download() parameters.
    session.trust_env = False
    return session


def _validate_known_hosts_file(known_hosts_file):
    """Reject entries that Paramiko would otherwise silently ignore."""
    with open(known_hosts_file, "r") as known_hosts:
        for line_number, line in enumerate(known_hosts, 1):
            entry_text = line.strip()
            if not entry_text or entry_text.startswith("#"):
                continue

            try:
                entry = paramiko.hostkeys.HostKeyEntry.from_line(
                    entry_text, line_number
                )
            except paramiko.hostkeys.InvalidHostKey as e:
                raise ValueError(
                    "Invalid host key entry in {} at line {}".format(
                        known_hosts_file, line_number
                    )
                ) from e

            if entry is None:
                raise ValueError(
                    "Invalid or unsupported host key entry in {} at line {}".format(
                        known_hosts_file, line_number
                    )
                )


def create_ssh_client():
    """Create an SSH client that rejects servers outside the trusted key stores."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

    for known_hosts_file in SSH_KNOWN_HOSTS_FILES:
        try:
            _validate_known_hosts_file(known_hosts_file)
            ssh.load_system_host_keys(known_hosts_file)
        except FileNotFoundError:
            continue

    return ssh


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
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host


def validate_http_url(remote_path, hostname, protocol):
    """
    Check that the HTTP(S) URL matches the declared peer and protocol.

    The caller controls remote_path, hostname, and protocol, so these checks enforce
    consistency rather than destination authorization. The HTTP(S) branch uses the
    complete URL while SFTP and SCP use hostname as the peer and remote_path as a path.
    Requiring the URL scheme and parsed host to match the separately declared values
    avoids ambiguous routing and sending credentials to a host different from the
    declared peer.

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

    # urllib.parse treats backslashes as hostname characters, while Requests
    # treats them as path separators. Reject the ambiguous authority while
    # continuing to allow backslashes in the path.
    if "\\" in url.netloc:
        return "URL authority in remote_path must not contain backslashes"

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

    try:
        prepared_url = requests.Request("GET", remote_path).prepare().url
        prepared_host = urlparse(prepared_url).hostname
    except (requests.exceptions.RequestException, UnicodeError, ValueError) as e:
        return "Malformed URL in remote_path: {}".format(e)

    if normalize_host(prepared_host) != declared_host:
        return (
            "URL host '{}' is interpreted as '{}' by the HTTP client, "
            "which does not match the requested hostname '{}'".format(
                url_host, prepared_host, hostname
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
                ssh = create_ssh_client()
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

                # Redirects are not followed because they can select a destination
                # different from the one validated above.
                dir_fd = None
                session = None
                try:
                    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    dir_flags |= getattr(os, "O_CLOEXEC", 0)
                    dir_fd = os.open(dir_path, dir_flags)
                    anchored_dir_stat = os.fstat(dir_fd)
                    if not (anchored_dir_stat.st_mode & stat.S_IWOTH):
                        return EXIT_FAILURE, (
                            "Directory is not world writable: {}".format(dir_path)
                        )

                    session = create_http_session()
                    response = session.get(
                        remote_path,
                        auth=auth,
                        stream=True,
                        timeout=HTTP_TIMEOUT,
                        allow_redirects=False,
                    )
                    try:
                        # Reject the whole 3xx range by status code rather than relying on
                        # Response.is_redirect, which only covers 301/302/303/307/308 and
                        # only when a Location header is present.
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("Location")
                            return EXIT_FAILURE, (
                                "Refusing redirect response {} from {}{}".format(
                                    response.status_code,
                                    remote_path,
                                    " to {}".format(location) if location else "",
                                )
                            )
                        response.raise_for_status()

                        _write_http_response(
                            response, local_path, dir_path, dir_fd
                        )
                    finally:
                        response.close()
                finally:
                    if session is not None:
                        session.close()
                    if dir_fd is not None:
                        os.close(dir_fd)
            elif protocol == "SCP":
                ssh = create_ssh_client()
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
