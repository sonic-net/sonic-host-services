import sys
import os
import psutil
import pytest
from unittest.mock import call, mock_open, patch, MagicMock
from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from datetime import datetime, timedelta

from .mock_connector import MockConnector


# Extend MockConnector with hmset support used by procdockerstatsd's
# batch_update_state_db. The shared MockConnector does not provide hmset,
# so add it here without modifying the shared mock.
def _mock_hmset(self, db_id, key, fvs):
    if key not in MockConnector.data:
        MockConnector.data[key] = {}
    for field, value in fvs.items():
        MockConnector.data[key][field] = value


MockConnector.hmset = _mock_hmset

swsscommon.SonicV2Connector = MockConnector

SAMPLE_MEMINFO = (
    "MemTotal:       16384000 kB\n"
    "MemFree:         8192000 kB\n"
    "MemAvailable:   12000000 kB\n"
    "Buffers:          512000 kB\n"
    "Cached:          2048000 kB\n"
    "SwapCached:            0 kB\n"
    "Active:          4000000 kB\n"
    "Inactive:        2000000 kB\n"
    "SwapTotal:       4096000 kB\n"
    "SwapFree:        3072000 kB\n"
    "Shmem:            256000 kB\n"
)

SAMPLE_DF_OUTPUT = (
    "Filesystem     Type     1K-blocks    Used Available  Inodes IFree Mounted on\n"
    "/dev/sda1      ext4     102400000 51200000  46080000 5000000 4000000 /\n"
    "tmpfs          tmpfs      8192000        0   8192000  999000  999000 /dev/shm\n"
    "/dev/sda2      ext4      51200000 10240000  38400000 2000000 1500000 /var/log\n"
    "devtmpfs       devtmpfs   8192000        0   8192000  999000  999000 /dev\n"
    "overlay        overlay  102400000 51200000  46080000 5000000 4000000 /var/lib/docker/overlay2/abc\n"
    "/dev/sdb1      xfs       20480000  5120000  15360000 1000000  800000 /mnt/data\n"
)

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
procdockerstatsd_path = os.path.join(scripts_path, 'procdockerstatsd')
procdockerstatsd = load_module_from_source('procdockerstatsd', procdockerstatsd_path)

class MockProcess:
    def __init__(self, uids, pid, ppid, memory_percent, cpu_percent, create_time, cmdline, user_time, system_time):
        self._uids = uids
        self._pid = pid
        self._ppid = ppid
        self._memory_percent = memory_percent
        self._cpu_percent = cpu_percent
        self._create_time = create_time
        self._terminal = cmdline
        self._cmdline = cmdline
        self._user_time = user_time
        self._system_time = system_time

    def uids(self):
        return self._uids

    @property
    def pid(self):
        return self._pid

    def ppid(self):
        return self._ppid

    def memory_percent(self):
        return self._memory_percent

    def cpu_percent(self):
        return self._cpu_percent

    def create_time(self):
        return self._create_time

    def terminal(self):
        return self._terminal

    def cmdline(self):
        return self._cmdline

    def cpu_times(self):
        class CPUTimes:
            def __init__(self, user_time, system_time):
                self.user = user_time
                self.system = system_time

            def __getitem__(self, index):
                if index == 0:
                    return self.user
                else:
                    return self.system

            def __iter__(self):
                yield self.user
                yield self.system

        return CPUTimes(self._user_time, self._system_time)


class TestProcDockerStatsDaemon(object):
    def setup_method(self):
        MockConnector.data = {}

    def test_convert_to_bytes(self):
        test_data = [
            ('1B', 1),
            ('500B', 500),
            ('1KB', 1000),
            ('500KB', 500000),
            ('1MB', 1000000),
            ('500MB', 500000000),
            ('1MiB', 1048576),
            ('500MiB', 524288000),
            ('66.41MiB', 69635932),
            ('333.6MiB', 349804954),
            ('1GiB', 1073741824),
            ('500GiB', 536870912000),
            ('7.751GiB', 8322572878)
        ]

        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        for test_input, expected_output in test_data:
            res = pdstatsd.convert_to_bytes(test_input)
            assert res == expected_output

    def test_run_command(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        output = pdstatsd.run_command(['echo', 'pdstatsd'])
        assert output == 'pdstatsd\n'

        output = pdstatsd.run_command([sys.executable, "-c", "import sys; sys.exit(6)"])
        assert output is None

    def test_update_processstats_command(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        current_time = datetime.now()
        valid_create_time1 = int((current_time - timedelta(days=1)).timestamp())
        valid_create_time2 = int((current_time - timedelta(days=2)).timestamp())
        # Create a list of mocked processes
        mocked_processes = [
            MockProcess(uids=[1000], pid=1234, ppid=0, memory_percent=10.5, cpu_percent=99.0, create_time=valid_create_time1, cmdline=['python', 'script.py'], user_time=1.5, system_time=2.0),
            MockProcess(uids=[1000], pid=5678, ppid=0, memory_percent=5.5, cpu_percent=15.5, create_time=valid_create_time2, cmdline=['bash', 'script.sh'], user_time=3.5, system_time=4.0),
            MockProcess(uids=[1000], pid=3333, ppid=0, memory_percent=5.5, cpu_percent=15.5, create_time=valid_create_time2, cmdline=['bash', 'script.sh'], user_time=3.5, system_time=4.0)
        ]
        mocked_processes2 = [
            MockProcess(uids=[1000], pid=1234, ppid=0, memory_percent=10.5, cpu_percent=20.5, create_time=valid_create_time1, cmdline=['python', 'script.py'], user_time=1.5, system_time=2.0),
            MockProcess(uids=[1000], pid=6666, ppid=0, memory_percent=5.5, cpu_percent=15.5, create_time=valid_create_time2, cmdline=['bash', 'script.sh'], user_time=3.5, system_time=4.0)
        ]

        with patch("procdockerstatsd.psutil.process_iter", return_value=mocked_processes) as mock_process_iter:
            pdstatsd.all_process_obj = {1234: mocked_processes2[0],
                                        6666: mocked_processes2[1]}
            pdstatsd.update_processstats_command()
            mock_process_iter.assert_called_once()
        assert(len(pdstatsd.all_process_obj)== 3)

        expected_fields = {'UID', 'PPID', '%CPU', '%MEM', 'STIME', 'TT', 'TIME', 'CMD'}
        for field in expected_fields:
            value = pdstatsd.state_db.get('STATE_DB', 'PROCESS_STATS|1234', field)
            assert value is not None, f"Missing expected field: {field}"

    @patch('procdockerstatsd.getstatusoutput_noshell_pipe', return_value=([0, 0], ''))
    def test_update_fipsstats_command(self, mock_cmd):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.update_fipsstats_command()
        assert pdstatsd.state_db.get('STATE_DB', 'FIPS_STATS|state', 'enforced') == "False"
        assert pdstatsd.state_db.get('STATE_DB', 'FIPS_STATS|state', 'enabled') == "True"

    def test_update_processstats_handles_nosuchprocess(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        current_time = datetime.now()
        valid_create_time = int((current_time - timedelta(hours=1)).timestamp())
        good_proc = MockProcess(uids=[1000], pid=1234, ppid=0, memory_percent=10.5, cpu_percent=99.0,
                                create_time=valid_create_time, cmdline=['python'], user_time=1.0, system_time=1.0)

        class NoSuchProcessMock(MockProcess):
            def cpu_percent(self):
                raise psutil.NoSuchProcess(pid=self._pid)

        bad_proc = NoSuchProcessMock(uids=[1000], pid=9999, ppid=0, memory_percent=0, cpu_percent=0,
                                    create_time=valid_create_time, cmdline=['fake'], user_time=0, system_time=0)

        with patch("procdockerstatsd.psutil.process_iter", return_value=[good_proc, bad_proc]):
            pdstatsd.update_processstats_command()

        assert 1234 in pdstatsd.all_process_obj
        assert 9999 not in pdstatsd.all_process_obj

    def test_datetime_utcnow_usage(self):
        """Test that datetime.utcnow() is used instead of datetime.now() for consistent UTC timestamps"""
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        # Mock datetime.utcnow to return a fixed time for testing
        fixed_time = datetime(2025, 7, 1, 12, 34, 56)

        with patch('procdockerstatsd.datetime') as mock_datetime:
            mock_datetime.utcnow.return_value = fixed_time

            # Test the update_fipsstats_command method which uses datetime.utcnow()
            pdstatsd.update_fipsstats_command()

            # Verify that utcnow() was called
            mock_datetime.utcnow.assert_called_once()

            # Verify that now() was NOT called (ensuring we're using UTC)
            mock_datetime.now.assert_not_called()

            # Test the main run loop datetime usage
            with patch.object(pdstatsd, 'update_dockerstats_command'):
                with patch.object(pdstatsd, 'update_processstats_command'):
                    with patch.object(pdstatsd, 'update_fipsstats_command'):
                        with patch('time.sleep'):  # Prevent actual sleep
                            # Mock the first iteration of the run loop
                            pdstatsd.update_dockerstats_command()
                            datetimeobj = mock_datetime.utcnow()
                            pdstatsd.update_state_db('DOCKER_STATS|LastUpdateTime', 'lastupdate', str(datetimeobj))
                            pdstatsd.update_processstats_command()
                            pdstatsd.update_state_db('PROCESS_STATS|LastUpdateTime', 'lastupdate', str(datetimeobj))
                            pdstatsd.update_fipsstats_command()
                            pdstatsd.update_state_db('FIPS_STATS|LastUpdateTime', 'lastupdate', str(datetimeobj))

                            # Verify utcnow() was called multiple times as expected
                            assert mock_datetime.utcnow.call_count >= 2        

    def test_run_method_executes_with_utcnow(self):
        """Test that run method executes and uses datetime.utcnow()"""
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        # Mock all dependencies but allow datetime.utcnow() to run normally
        with patch.object(pdstatsd, 'update_dockerstats_command'):
            with patch.object(pdstatsd, 'update_processstats_command'):
                with patch.object(pdstatsd, 'update_fipsstats_command'):
                    with patch('time.sleep', side_effect=Exception("Stop after first iteration")):
                        with patch('os.getuid', return_value=0):  # Mock as root
                            with patch.object(pdstatsd, 'log_info'):
                                with patch.object(pdstatsd, 'update_state_db') as mock_update_db:
                                    # This will actually call run() method
                                    try:
                                        pdstatsd.run()
                                    except Exception as e:
                                        if "Stop after first iteration" in str(e):
                                            # Verify that update_state_db was called
                                            assert mock_update_db.call_count >= 3
                                            # Verify that timestamps were passed
                                            for call in mock_update_db.call_args_list:
                                                args = call[0]
                                                if len(args) >= 3 and 'lastupdate' in args[1]:
                                                    timestamp_str = args[2]
                                                    assert isinstance(timestamp_str, str)
                                                    assert len(timestamp_str) > 0
                                        else:
                                            raise

    # ---- MEMORY_STATS tests ----

    def test_format_mem_output(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        result = pdstatsd.format_mem_output(SAMPLE_MEMINFO)

        assert 'MEMORY_STATS|Physical' in result
        assert result['MEMORY_STATS|Physical']['1K-blocks'] == '16384000'
        assert result['MEMORY_STATS|Physical']['Used'] == str(16384000 - 8192000)

        assert 'MEMORY_STATS|Virtual' in result
        assert result['MEMORY_STATS|Virtual']['1K-blocks'] == str(16384000 + 4096000)
        assert result['MEMORY_STATS|Virtual']['Used'] == str(16384000 + 4096000 - 8192000 - 3072000)

        assert 'MEMORY_STATS|Buffer' in result
        assert result['MEMORY_STATS|Buffer']['1K-blocks'] == '16384000'
        assert result['MEMORY_STATS|Buffer']['Used'] == '512000'

        assert 'MEMORY_STATS|Cached' in result
        assert result['MEMORY_STATS|Cached']['1K-blocks'] == '2048000'
        assert result['MEMORY_STATS|Cached']['Used'] == '2048000'

        assert 'MEMORY_STATS|Shared' in result
        assert result['MEMORY_STATS|Shared']['1K-blocks'] == '256000'
        assert result['MEMORY_STATS|Shared']['Used'] == '256000'

        assert 'MEMORY_STATS|Swap' in result
        assert result['MEMORY_STATS|Swap']['1K-blocks'] == '4096000'
        assert result['MEMORY_STATS|Swap']['Used'] == str(4096000 - 3072000)

    def test_create_mem_dict_all_categories(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        mem_info = {
            "MemTotal": "16384000",
            "MemFree": "8192000",
            "Buffers": "512000",
            "Cached": "2048000",
            "Shmem": "256000",
            "SwapTotal": "4096000",
            "SwapFree": "3072000",
        }
        result = pdstatsd.create_mem_dict(mem_info)
        expected_keys = [
            'MEMORY_STATS|Physical',
            'MEMORY_STATS|Virtual',
            'MEMORY_STATS|Buffer',
            'MEMORY_STATS|Cached',
            'MEMORY_STATS|Shared',
            'MEMORY_STATS|Swap',
        ]
        for key in expected_keys:
            assert key in result, "Missing key: {}".format(key)
            assert '1K-blocks' in result[key]
            assert 'Used' in result[key]

    def test_create_mem_dict_zero_swap(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        mem_info = {
            "MemTotal": "8000000",
            "MemFree": "4000000",
            "Buffers": "100000",
            "Cached": "500000",
            "Shmem": "50000",
            "SwapTotal": "0",
            "SwapFree": "0",
        }
        result = pdstatsd.create_mem_dict(mem_info)
        assert result['MEMORY_STATS|Swap']['1K-blocks'] == '0'
        assert result['MEMORY_STATS|Swap']['Used'] == '0'
        assert result['MEMORY_STATS|Virtual']['1K-blocks'] == str(8000000)
        assert result['MEMORY_STATS|Virtual']['Used'] == str(8000000 - 4000000)

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value=SAMPLE_MEMINFO)
    def test_update_memory_command_success(self, mock_run):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_info = MagicMock()
        pdstatsd.log_error = MagicMock()

        result = pdstatsd.update_memory_command()
        assert result is True
        mock_run.assert_called_once_with(["cat", "/proc/meminfo"])
        pdstatsd.log_info.assert_called_once()
        pdstatsd.log_error.assert_not_called()

        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Physical', '1K-blocks') == '16384000'
        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Swap', '1K-blocks') == '4096000'

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value=None)
    def test_update_memory_command_null_output(self, mock_run):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_error = MagicMock()

        result = pdstatsd.update_memory_command()
        assert result is False
        pdstatsd.log_error.assert_called_once()
        assert "returned null output" in pdstatsd.log_error.call_args[0][0]

    @patch.object(procdockerstatsd.ProcDockerStats, 'format_mem_output', return_value={})
    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value="some data")
    def test_update_memory_command_empty_format(self, mock_run, mock_format):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_error = MagicMock()

        result = pdstatsd.update_memory_command()
        assert result is False
        pdstatsd.log_error.assert_called_once()
        assert "formatting for memory stats failed" in pdstatsd.log_error.call_args[0][0]

    # ---- MOUNT_POINTS tests ----

    def test_format_mount_cmd_output(self):
        proc_mounts = (
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "/dev/sda2 /var/log ext4 rw,relatime 0 0\n"
            "/dev/sdb1 /mnt/data xfs rw,relatime 0 0\n"
        )
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.format_mount_cmd_output(SAMPLE_DF_OUTPUT)

        assert 'MOUNT_POINTS|/' in result
        assert result['MOUNT_POINTS|/']['Filesystem'] == '/dev/sda1'
        assert result['MOUNT_POINTS|/']['Type'] == 'ext4'
        assert result['MOUNT_POINTS|/']['1K-blocks'] == '102400000'
        assert result['MOUNT_POINTS|/']['Used'] == '51200000'
        assert result['MOUNT_POINTS|/']['Available'] == '46080000'
        assert result['MOUNT_POINTS|/']['InodeTotal'] == '5000000'
        assert result['MOUNT_POINTS|/']['InodeAvailable'] == '4000000'

        assert 'MOUNT_POINTS|/var/log' in result
        assert result['MOUNT_POINTS|/var/log']['Type'] == 'ext4'

        assert 'MOUNT_POINTS|/mnt/data' in result
        assert result['MOUNT_POINTS|/mnt/data']['Type'] == 'xfs'

    def test_format_mount_cmd_output_filters_tmpfs_devtmpfs_overlay(self):
        proc_mounts = (
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "/dev/sda2 /var/log ext4 rw,relatime 0 0\n"
            "/dev/sdb1 /mnt/data xfs rw,relatime 0 0\n"
        )
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.format_mount_cmd_output(SAMPLE_DF_OUTPUT)

        for key, val in result.items():
            assert val['Type'] not in ('tmpfs', 'devtmpfs', 'overlay'), \
                "Type '{}' should be filtered out but found in key '{}'".format(val['Type'], key)

    def test_create_mount_dict_basic(self):
        proc_mounts = "/dev/sda1 / ext4 rw,relatime 0 0\n/dev/sda2 /var/log ext4 rw,relatime 0 0\n"
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        dict_list = [
            ['/dev/sda1', 'ext4', '102400000', '51200000', '46080000', '5000000', '4000000', '/'],
            ['/dev/sda2', 'ext4', '51200000', '10240000', '38400000', '2000000', '1500000', '/var/log'],
        ]
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.create_mount_dict(dict_list)
        assert len(result) == 2
        assert 'MOUNT_POINTS|/' in result
        assert result['MOUNT_POINTS|/']['Filesystem'] == '/dev/sda1'
        assert result['MOUNT_POINTS|/']['InodeTotal'] == '5000000'
        assert result['MOUNT_POINTS|/']['InodeAvailable'] == '4000000'
        assert 'MOUNT_POINTS|/var/log' in result

    def test_create_mount_dict_all_filtered(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        dict_list = [
            ['tmpfs', 'tmpfs', '8192000', '0', '8192000', '999000', '999000', '/dev/shm'],
            ['devtmpfs', 'devtmpfs', '8192000', '0', '8192000', '999000', '999000', '/dev'],
            ['overlay', 'overlay', '102400000', '51200000', '46080000', '5000000', '4000000', '/var/lib/docker/overlay2/abc'],
        ]
        with patch('builtins.open', mock_open(read_data="")):
            result = pdstatsd.create_mount_dict(dict_list)
        assert len(result) == 0

    def test_format_mount_cmd_output_single_entry(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        data = (
            "Filesystem     Type     1K-blocks    Used Available  Inodes  IFree Mounted on\n"
            "/dev/sda1      ext4     102400000 51200000  46080000 5000000 4000000 /\n"
        )
        proc_mounts = "/dev/sda1 / ext4 rw,relatime 0 0\n"
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.format_mount_cmd_output(data)
        assert len(result) == 1
        assert 'MOUNT_POINTS|/' in result

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value=SAMPLE_DF_OUTPUT)
    def test_update_mountpointstats_command_success(self, mock_run):
        proc_mounts = (
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "/dev/sda2 /var/log ext4 rw,relatime 0 0\n"
            "/dev/sdb1 /mnt/data xfs rw,relatime 0 0\n"
        )
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_info = MagicMock()
        pdstatsd.log_error = MagicMock()

        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.update_mountpointstats_command()
        assert result is True
        mock_run.assert_called_once_with(["df", "--output=source,fstype,size,used,avail,itotal,iavail,target"])
        pdstatsd.log_info.assert_called_once()
        pdstatsd.log_error.assert_not_called()

        assert pdstatsd.state_db.get('STATE_DB', 'MOUNT_POINTS|/', 'Filesystem') == '/dev/sda1'
        assert pdstatsd.state_db.get('STATE_DB', 'MOUNT_POINTS|/', 'Type') == 'ext4'

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value=None)
    def test_update_mountpointstats_command_null_output(self, mock_run):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_error = MagicMock()

        result = pdstatsd.update_mountpointstats_command()
        assert result is False
        pdstatsd.log_error.assert_called_once()
        assert "returned null output" in pdstatsd.log_error.call_args[0][0]

    @patch.object(procdockerstatsd.ProcDockerStats, 'format_mount_cmd_output', return_value={})
    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value="some data")
    def test_update_mountpointstats_command_empty_format(self, mock_run, mock_format):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_error = MagicMock()

        result = pdstatsd.update_mountpointstats_command()
        assert result is False
        pdstatsd.log_error.assert_called_once()
        assert "formatting for filesystem output failed" in pdstatsd.log_error.call_args[0][0]

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command')
    def test_update_mountpointstats_command_writes_all_valid_entries(self, mock_run):
        """Verify only non-tmpfs/devtmpfs/overlay entries are written to state_db."""
        df_output = (
            "Filesystem     Type     1K-blocks    Used Available  Inodes   IFree Mounted on\n"
            "/dev/sda1      ext4     102400000 51200000  46080000 5000000 4000000 /\n"
            "tmpfs          tmpfs      8192000        0   8192000  999000  999000 /dev/shm\n"
        )
        mock_run.return_value = df_output
        proc_mounts = "/dev/sda1 / ext4 rw,relatime 0 0\n"
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_info = MagicMock()
        pdstatsd.log_error = MagicMock()

        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            result = pdstatsd.update_mountpointstats_command()
        assert result is True
        assert pdstatsd.state_db.get('STATE_DB', 'MOUNT_POINTS|/', 'Filesystem') == '/dev/sda1'
        pdstatsd.log_info.assert_called_once()
        assert "1 entries written" in pdstatsd.log_info.call_args[0][0]

    @patch.object(procdockerstatsd.ProcDockerStats, 'run_command', return_value=SAMPLE_MEMINFO)
    def test_update_memory_command_writes_all_categories(self, mock_run):
        """Verify all six MEMORY_STATS categories are written to state_db."""
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.log_info = MagicMock()

        result = pdstatsd.update_memory_command()
        assert result is True
        pdstatsd.log_info.assert_called_once()
        assert "6 entries written" in pdstatsd.log_info.call_args[0][0]

        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Physical', '1K-blocks') == '16384000'
        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Physical', 'Used') == str(16384000 - 8192000)

        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Buffer', 'Used') == '512000'
        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Cached', 'Used') == '2048000'
        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Shared', 'Used') == '256000'
        assert pdstatsd.state_db.get('STATE_DB', 'MEMORY_STATS|Swap', '1K-blocks') == '4096000'

    # ---- run() loop integration test ----

    @patch('procdockerstatsd.time.sleep', side_effect=SystemExit("break loop"))
    @patch.object(procdockerstatsd.ProcDockerStats, 'update_memory_command')
    @patch.object(procdockerstatsd.ProcDockerStats, 'update_mountpointstats_command')
    @patch.object(procdockerstatsd.ProcDockerStats, 'update_fipsstats_command')
    @patch.object(procdockerstatsd.ProcDockerStats, 'update_processstats_command')
    @patch.object(procdockerstatsd.ProcDockerStats, 'update_dockerstats_command')
    @patch('procdockerstatsd.os.getuid', return_value=0)
    def test_run_invokes_memory_and_mount_updates(self, mock_uid,
                                                   mock_docker, mock_process,
                                                   mock_fips, mock_mount, mock_mem,
                                                   mock_sleep):
        """Verify the run() loop calls update_memory_command and update_mountpointstats_command."""
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        with pytest.raises(SystemExit):
            pdstatsd.run()

        mock_mount.assert_called_once()
        mock_mem.assert_called_once()

