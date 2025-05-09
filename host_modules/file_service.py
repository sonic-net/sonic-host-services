"""File stat handler"""

from host_modules import host_service
import subprocess

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

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='ia{ss}')
    def remove_file(self, path):
        if not path:
            return EXIT_FAILURE, {'error': 'Dbus remove_file called with no path specified'}

        try:
            os.remove(path)
            return 0, {"message": f"File {path} removed successfully"}
        except FileNotFoundError:
            return 1, {"error": f"File not found: {path}"}
        except Exception as e:
            return 1, {"error": str(e)}
