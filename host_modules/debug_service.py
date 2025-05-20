import pty
import threading
import subprocess
import os
import select

from host_modules import host_service

MOD_NAME = 'DebugExecutor'
INTERFACE = host_service.bus_name(MOD_NAME)

class DebugExecutor(host_service.HostModule):
    """
    Debug container command handler.
    Allows the debug container to execute arbitrary commands on the device, after having been validated against the whitelist.
    """

    def _run_and_stream(self, command):
        """
        Internal method to asynchronously run a command and stream stdout/stderr to the requesting client.
        """
        master_fd, slave_fd = pty.openpty()

        p = subprocess.Popen(
            command,
            stdin = slave_fd,
            stdout = slave_fd,
            stderr = subprocess.PIPE,
            close_fds = True,
            bufsize = 0,
            universal_newlines = False
        )
        os.close(slave_fd)
        if p.stderr == None:
            return

        stderr_fd = p.stderr.fileno()
        fds = [master_fd, stderr_fd]

        try:
            while True:
                ready, _, _ = select.select(fds, [], [])
                if master_fd in ready:
                    data = os.read(master_fd, 4096)
                    if not data:
                        fds.remove(master_fd)
                    else:
                        # decode and emit
                        self.Stdout(data.decode(errors='ignore'))

                if stderr_fd in ready:
                    data = os.read(stderr_fd, 4096)
                    if not data:
                        fds.remove(stderr_fd)
                    else:
                        self.Stderr(data.decode(errors='ignore'))

                # If both fds closed, break
                if not fds:
                    break

        finally:
            # Ensure the process has exited
            rc = p.wait()
            os.close(master_fd)
            p.stderr.close()

            self.ExitCode(rc)


    @host_service.signal(INTERFACE, signature='s')
    def Stdout(self, data):
        """
        Signal to emit a line of stdout for a given command.
        """
        pass

    @host_service.signal(INTERFACE, signature='s')
    def Stderr(self, data):
        """
        Signal to emit a line of stderr for a given command.
        """
        pass

    @host_service.signal(INTERFACE, signature='i')
    def ExitCode(self, data):
        """
        Signal to emit the exit code of the command, after completion.
        """
        pass

    @host_service.method(INTERFACE, in_signature='s')
    def RunCommand(self, command):
        """
        DBus endpoint - receives a command, and streams the response data back to the client.
        Immediately returns, allowing thread to be solely responsible for sending responses.
        """
        threading.Thread(target=self._run_and_stream, args=(command,), daemon=True).start()
