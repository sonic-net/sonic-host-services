import pty
import subprocess
import os
import select
import errno

from concurrent.futures import ThreadPoolExecutor, TimeoutError

from host_modules import host_service

EXCEPTION_RAISED = 1
MOD_NAME = 'DebugExecutor'
INTERFACE = host_service.bus_name(MOD_NAME)


class DebugExecutor(host_service.HostModule):
    """
    Debug container command handler.
    Allows the debug container to execute arbitrary commands on the device, after having been validated against the whitelist.
    """

    def __init__(self, mod_name):
        super().__init__(mod_name)
        self.executor = ThreadPoolExecutor(max_workers=1)

    def _run_and_stream(self, argv):
        """
        Internal method to asynchronously run a command and stream stdout/stderr to the requesting client.
        """
        master_fd, slave_fd = pty.openpty()

        p = subprocess.Popen(
            argv,
            stdin = slave_fd,
            stdout = slave_fd,
            stderr = subprocess.PIPE,
            close_fds = True,
            bufsize = 0,
            universal_newlines = False
        )
        os.close(slave_fd)
        if p.stderr == None:
            raise Exception("Could not open pipe for stderr")

        stderr_fd = p.stderr.fileno()
        fds = [master_fd, stderr_fd]

        try:
            while True:
                ready, _, _ = select.select(fds, [], [])
                if master_fd in ready:
                    # Master FD is a PTY, will throw exception when closed
                    try:
                        data = os.read(master_fd, 4096)
                        self.Stdout(data.decode(errors='ignore'))
                    except OSError as e:
                        if e.errno == errno.EIO:
                            fds.remove(master_fd)
                            os.close(master_fd)
                        else:
                            raise

                if stderr_fd in ready:
                    # Stderr FD is a normal fd, will be empty when closed
                    data = os.read(stderr_fd, 4096)
                    if not data:
                        fds.remove(stderr_fd)
                        p.stderr.close()
                    else:
                        self.Stderr(data.decode(errors='ignore'))

                if not fds:
                    break

        finally:
            rc = p.wait()
            return rc

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

    @host_service.method(INTERFACE, in_signature='as', out_signature='is')
    def RunCommand(self, argv):
        """
        DBus endpoint - receives a command, and streams the response data back to the client.
        Starts the command in a separate thread, with a generous timeout once the command has begun execution.

        The thread pool has a limit of 1, to ensure that only one user at a time may execute commands on the device.

        Returns a tuple, consisting of (int_return_code, string_details)
        """
        future = self.executor.submit(self._run_and_stream, argv)
        try:
            rc = future.result(timeout=10*60)

            return (rc, f"Command exited with {rc}")
        except Exception as e:
            return (EXCEPTION_RAISED, f"Exception raised: {e}")
