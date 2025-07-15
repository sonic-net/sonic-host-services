import errno
import os
import select
import subprocess
from unittest import TestCase, mock

from host_modules.debug_service import DebugExecutor, MOD_NAME


class TestDebugExecutor(TestCase):
    """
    Unit tests for the DebugExecutor module.
    """

    @mock.patch("threading.Event")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_command_submits_to_thread_pool(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_event,
    ):
        """
        Verify that RunCommand correctly starts a new thread
        to handle the command execution.
        """
        executor = DebugExecutor(MOD_NAME)
        executor.executor.submit = mock.Mock()
        argv = ["ls", "-l"]

        executor.RunCommand(argv)

        # Check that a thread was created with the correct target and arguments
        executor.executor.submit.assert_called_once()

    @mock.patch("threading.Event")
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
        mock_event,
    ):
        """
        Test the full, successful execution of a command,
        capturing stdout, stderr, and the exit code.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        stderr_fd = 12
        mock_openpty.return_value = (master_fd, slave_fd)
        mock_event.is_set.return_value = False

        # Mock the subprocess object
        mock_proc = mock.Mock()
        mock_proc.stderr = mock.Mock()
        mock_proc.stderr.fileno.return_value = stderr_fd
        mock_proc.wait.return_value = 0  # Successful exit code
        mock_proc.poll.return_value = 0 
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

        argv = ["ls", "-la"]
        rc = executor._run_and_stream(argv, mock_event)

        # --- Assertions ---
        # Verify exit code is correctly returned
        assert rc == 0, f"Return code {rc} incorrect"

        # Verify that the process was started correctly
        expected_env = os.environ.copy()
        expected_env['TERM'] = 'xterm'

        mock_popen.assert_called_once_with(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            universal_newlines=False,
            env=expected_env
        )

        # Verify stdout and stderr signals were emitted with correct data
        executor.Stdout.assert_called_once_with(stdout_data.decode())
        executor.Stderr.assert_called_once_with(stderr_data.decode())
        mock_proc.poll.assert_called()

        # Verify that file descriptors were closed
        mock_os_close.assert_any_call(master_fd)
        mock_os_close.assert_any_call(slave_fd)
        mock_os_close.assert_any_call(stderr_fd)

    @mock.patch("threading.Event")
    @mock.patch("select.select")
    @mock.patch("os.read")
    @mock.patch("os.close")
    @mock.patch("subprocess.Popen")
    @mock.patch("pty.openpty")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_and_stream_failure(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_openpty,
        mock_popen,
        mock_os_close,
        mock_os_read,
        mock_select,
        mock_event,
    ):
        """
        Test the full, successful execution of a command,
        capturing stdout, stderr, and the exit code.

        In this instance, the command fails.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        stderr_fd = 12
        mock_openpty.return_value = (master_fd, slave_fd)
        mock_event.is_set.return_value = False

        # Mock the subprocess object
        mock_proc = mock.Mock()
        mock_proc.stderr = mock.Mock()
        mock_proc.stderr.fileno.return_value = stderr_fd
        mock_proc.wait.return_value = 1  # Failure exit code
        mock_proc.poll.return_value = 1 
        mock_popen.return_value = mock_proc

        # Mock the I/O multiplexing and reads
        stderr_data = b"ls: cannot access '/nonexistent/dir': No such file or directory"

        # Define the sequence of events for select and os.read
        mock_select.side_effect = [
            ([stderr_fd], [], []),  # 1. stderr is ready
            ([master_fd], [], []),  # 2. master pty is closed
            ([stderr_fd], [], []),  # 3. stderr pipe is closed
        ]
        mock_os_read.side_effect = [
            stderr_data,  # Corresponds to select call #1
            OSError(errno.EIO, "I/O error"),  # Corresponds to select call #2
            b"",  # Corresponds to select call #3 (EOF on stderr)
        ]

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Attach mocks to the signal methods to spy on them
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()

        argv = ["ls", "-la", "/nonexistant/dir"]
        rc = executor._run_and_stream(argv, mock_event)

        # --- Assertions ---
        # Verify exit code is correctly returned
        assert rc == 1, f"Return code {rc} incorrect"

        # Verify that the process was started correctly
        expected_env = os.environ.copy()
        expected_env['TERM'] = 'xterm'

        mock_popen.assert_called_once_with(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            universal_newlines=False,
            env=expected_env
        )

        # Verify stdout and stderr signals were emitted with correct data
        executor.Stderr.assert_called_once_with(stderr_data.decode())
        mock_proc.poll.assert_called()

        # Verify that file descriptors were closed
        mock_os_close.assert_any_call(master_fd)
        mock_os_close.assert_any_call(slave_fd)
        mock_os_close.assert_any_call(stderr_fd)

    @mock.patch("threading.Event")
    @mock.patch("select.select")
    @mock.patch("os.close")
    @mock.patch("subprocess.Popen")
    @mock.patch("pty.openpty")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_and_stream_cancelled(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_openpty,
        mock_popen,
        mock_os_close,
        mock_select,
        mock_event,
    ):
        """
        Test that an early cancellation is properly propagated
        to the subprocess, and that cleanup is executed.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        stderr_fd = 12
        mock_openpty.return_value = (master_fd, slave_fd)
        mock_event.is_set.return_value = True

        # Mock the subprocess object
        mock_proc = mock.Mock()
        mock_proc.stderr = mock.Mock()
        mock_proc.stderr.fileno.return_value = stderr_fd
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        mock_select.return_value = [([], [], [])]

        # First raise an exception, then return 0
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired("N/A", 0),
            0
        ]

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Attach mocks to the signal methods to spy on them
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()

        argv = ["/bin/test_command", "--arg"]
        rc = executor._run_and_stream(argv, mock_event)

        # --- Assertions ---
        # Verify exit code is correctly returned
        assert rc == 0, f"Return code {rc} incorrect"

        # Verify that the process was started correctly
        expected_env = os.environ.copy()
        expected_env['TERM'] = 'xterm'

        mock_popen.assert_called_once_with(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            close_fds=True,
            bufsize=0,
            universal_newlines=False,
            env=expected_env
        )

        # Verify stdout and stderr signals were not emitted
        executor.Stdout.assert_not_called()
        executor.Stderr.assert_not_called()
        mock_proc.poll.assert_called()

        # Verify that file descriptors were closed
        mock_os_close.assert_any_call(master_fd)
        mock_os_close.assert_any_call(slave_fd)
        mock_os_close.assert_any_call(stderr_fd)

        # Verify that the process was cleaned up
        mock_proc.terminate.assert_called()
        mock_proc.kill.assert_called()

    @mock.patch("threading.Event")
    @mock.patch("os.close")
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
        mock_popen,
        mock_os_close,
        mock_event,
    ):
        """
        Test that _run_and_stream exits early if the subprocess fails to create a stderr pipe.
        """
        # --- Mock setup ---
        master_fd, slave_fd = 10, 11
        mock_openpty.return_value = (master_fd, slave_fd)
        mock_event.is_set.return_value = False

        # Mock a process where stderr is None
        mock_proc = mock.Mock()
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Spy on the signal methods
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()

        with mock.patch.object(select, 'select') as mock_select:
            with self.assertRaises(Exception) as context:
                executor._run_and_stream(["some_command"], mock_event)

            # select should not be called if the function returns early
            mock_select.assert_not_called()

        # The function should return without emitting any signals
        executor.Stdout.assert_not_called()
        executor.Stderr.assert_not_called()

        # Verify that opened file descriptors were closed
        mock_os_close.assert_any_call(slave_fd)

    @mock.patch("pty.openpty")
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_run_command_exception(
        self,
        mock_init,
        mock_bus_name,
        mock_system_bus,
        mock_openpty,
    ):
        """
        Test that any exception thrown by the DBUS endpoint is caught,
        and correctly returns errno.EIO
        """
        # --- Mock setup ---
        mock_openpty.return_value = Exception()

        # --- Execution ---
        executor = DebugExecutor(MOD_NAME)
        # Attach mocks to the signal methods to spy on them
        executor.Stdout = mock.Mock()
        executor.Stderr = mock.Mock()

        argv = ["/bin/test_command", "--arg"]
        rc, _ = executor.RunCommand(argv)

        # --- Assertions ---
        # Verify exit code is correctly returned
        assert rc == errno.EIO, f"Return code '{rc}' does not match expected code '{errno.EIO}'"

        # Verify stdout and stderr signals were emitted with correct data
        executor.Stdout.assert_not_called()
        executor.Stderr.assert_not_called()
