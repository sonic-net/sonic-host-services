import errno
import os
import pty
import select
import subprocess
import threading
from unittest import mock

from host_modules.debug_service import DebugExecutor, MOD_NAME


class TestDebugExecutor(object):
    """
    Unit tests for the DebugExecutor module.
    """

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_command_starts_thread(self, mock_init, mock_bus_name, mock_system_bus):
        """
        Verify that RunCommand correctly starts a new thread
        to handle the command execution.
        """
        executor = DebugExecutor(MOD_NAME)
        argv = ["ls", "-l"]

        with mock.patch.object(threading, "Thread") as mock_thread:
            executor.RunCommand(argv)

            # Check that a thread was created with the correct target and arguments
            mock_thread.assert_called_once_with(
                target=executor._run_and_stream,
                args=(argv,),
                daemon=True
            )
            # Check that the thread was started
            mock_thread.return_value.start.assert_called_once()

    @mock.patch("select.select")
    @mock.patch("os.read")
    @mock.patch("os.close")
    @mock.patch("subprocess.Popen")
    @mock.patch("pty.openpty")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_and_stream_success(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_openpty,
        mock_popen,
        mock_os_close,
        mock_os_read,
        mock_select,
    ):
        """
        Test the full, successful execution of a command,
        capturing stdout, stderr, and the exit code.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        stderr_fd = 12
        mock_openpty.return_value = (master_fd, slave_fd)

        # Mock the subprocess object
        mock_proc = mock.Mock()
        mock_proc.stderr = mock.Mock()
        mock_proc.stderr.fileno.return_value = stderr_fd
        mock_proc.wait.return_value = 0  # Successful exit code
        mock_popen.return_value = mock_proc

        # Mock the I/O multiplexing and reads
        stdout_data = b"this is stdout"
        stderr_data = b"this is stderr"

        # Define the sequence of events for select and os.read
        mock_select.side_effect = [
            ([master_fd], [], []),  # 1. stdout is ready
            ([stderr_fd], [], []),  # 2. stderr is ready
            ([master_fd], [], []),  # 3. master pty is closed
            ([stderr_fd], [], []),  # 4. stderr pipe is closed
        ]
        mock_os_read.side_effect = [
            stdout_data,  # Corresponds to select call #1
            stderr_data,  # Corresponds to select call #2
            OSError(errno.EIO, "I/O error"),  # Corresponds to select call #3
            b"",  # Corresponds to select call #4 (EOF on stderr)
        ]

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Attach mocks to the signal methods to spy on them
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()
        executor.ExitCode = mock.Mock()

        argv = ["/bin/test_command", "--arg"]
        executor._run_and_stream(argv)

        # --- Assertions ---
        # Verify that the process was started correctly
        mock_popen.assert_called_once_with(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            universal_newlines=False,
        )

        # Verify stdout, stderr, and exit code signals were emitted with correct data
        executor.Stdout.assert_called_once_with(stdout_data.decode())
        executor.Stderr.assert_called_once_with(stderr_data.decode())
        mock_proc.wait.assert_called_once()
        executor.ExitCode.assert_called_once_with(0)

        # Verify that file descriptors were closed
        mock_os_close.assert_any_call(slave_fd)
        mock_proc.stderr.close.assert_called_once()

    @mock.patch("subprocess.Popen")
    @mock.patch("pty.openpty")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_and_stream_no_stderr(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_openpty,
        mock_popen
    ):
        """
        Test that _run_and_stream exits early if the subprocess fails to create a stderr pipe.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        mock_openpty.return_value = (master_fd, slave_fd)

        # Mock a process where stderr is None
        mock_proc = mock.Mock()
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Spy on the signal methods
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()
        executor.ExitCode = mock.Mock()

        with mock.patch.object(select, 'select') as mock_select:
             executor._run_and_stream(["some_command"])
             # select should not be called if the function returns early
             mock_select.assert_not_called()

        # The function should return without emitting any signals
        executor.Stdout.assert_not_called()
        executor.Stderr.assert_not_called()
        executor.ExitCode.assert_not_called()
