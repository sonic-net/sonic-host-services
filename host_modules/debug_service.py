import pty
import subprocess
import os
import select
import errno
import logging

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from host_modules import host_service

# Timeout should be slightly less than default DBUS timeout (25 sec)
TIMEOUT = 20
MOD_NAME = 'DebugExecutor'
INTERFACE = host_service.bus_name(MOD_NAME)
logger = logging.getLogger(__name__)


class DebugExecutor(host_service.HostModule):
    """
    Debug container command handler.
    Allows the debug container to execute arbitrary commands on the device, after having been validated against the whitelist.
    """

    def __init__(self, mod_name):
        super().__init__(mod_name)
        self.executor = ThreadPoolExecutor(max_workers=1)

    def _run_and_stream(self, argv, cancellation_event):
        """
        Internal method to asynchronously run a command and stream stdout/stderr to the requesting client.
        """
        master_fd, slave_fd = pty.openpty()

        # Populate an environment for interactive commands (i.e. 'top')
        env = os.environ.copy()
        env['TERM'] = 'xterm'

        p = subprocess.Popen(
            argv,
            stdin = slave_fd,
            stdout = slave_fd,
            stderr = subprocess.PIPE,
            close_fds = True,
            bufsize = 0,
            universal_newlines = False,
            env = env,
        )
        os.close(slave_fd)
        if p.stderr == None:
            raise Exception("Could not open pipe for stderr")

        stderr_fd = p.stderr.fileno()
        fds = [master_fd, stderr_fd]

        try:
            while True:
                ready, _, _ = select.select(fds, [], [])

                # Terminate this process if calling thread exits
                if cancellation_event.is_set():
                    break

                if master_fd in ready:
                    # Master FD is a PTY, will throw exception when closed
                    try:
                        data = os.read(master_fd, 4096)
                        self.Stdout(data.decode(errors='ignore'))
                    except OSError as e:
                        if e.errno == errno.EIO:
                            fds.remove(master_fd)
                        else:
                            raise

                if stderr_fd in ready:
                    # Stderr FD is a normal fd, will be empty when closed
                    data = os.read(stderr_fd, 4096)
                    if not data:
                        fds.remove(stderr_fd)
                    else:
                        self.Stderr(data.decode(errors='ignore'))

                if not fds:
                    break

        finally:
            os.close(master_fd)
            os.close(stderr_fd)

            # Check if the process is still running before trying to stop it
            if p.poll() is None:
                logger.info(f"Terminating subprocess (PID: {p.pid}) for command '{argv}'...")
                p.terminate()
                try:
                    rc = p.wait(timeout=5)
                    logger.info(f"Subprocess for '{argv}' terminated gracefully with code: {rc}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Process for '{argv}' did not terminate gracefully. Forcing kill...")
                    p.kill()
                    rc = p.wait()
                    logger.info(f"Subprocess for '{argv}' was forcefully killed, exited with code: {rc}")
            else:
                rc = p.poll()

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
        Starts the command in a separate thread, with a timeout once the command has begun execution.

        The thread pool has a limit of 1, to ensure that only one user at a time may execute commands on the device.
        Additionally, the timeout ensures that commands are stopped once the default DBUS timeout has been reached.

        Returns a tuple, consisting of (int_return_code, string_details)
        """
        logger.info(f"Running command: '{argv}'")
        cancellation_event = Event()
        future = self.executor.submit(self._run_and_stream, argv, cancellation_event)
        try:
            rc = future.result(timeout=TIMEOUT)
            logger.info(f"Command '{argv}' exited with code: {rc}")

            return (rc, f"Command exited with {rc}")
        except TimeoutError as e:
            err_msg = f"TimeoutError: Command '{argv}' took longer than {TIMEOUT} sec to complete"
            logger.error(err_msg)

            cancellation_event.set()

            return (errno.ETIMEDOUT, err_msg)
        except Exception as e:
            exception_type = type(e).__name__
            err_details = str(e) if str(e) else 'No details within error message'

            err_msg = f"{exception_type}: Command '{argv}' caused exception to be thrown: {err_details}"
            logger.error(err_msg)

            cancellation_event.set()

            return (errno.EIO, err_msg)
