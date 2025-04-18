import sys
import os
import psutil
import pytest
from unittest.mock import call, patch
from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from datetime import datetime, timedelta

from .mock_connector import MockConnector

swsscommon.SonicV2Connector = MockConnector

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

    @patch('procdockerstatsd.getstatusoutput_noshell_pipe', return_value=([0, 0], ''))
    def test_update_fipsstats_command(self, mock_cmd):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        pdstatsd.update_fipsstats_command()
        assert pdstatsd.state_db.get('STATE_DB', 'FIPS_STATS|state', 'enforced') == "False"
        assert pdstatsd.state_db.get('STATE_DB', 'FIPS_STATS|state', 'enabled') == "True"

    def test_create_mount_dict_pass(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        mountpoint_dict = [["udev", "ext-4", "10000", "1000", "9000", "", "/dev"]]
        mountpoint_dict_return = pdstatsd.create_mount_dict(mountpoint_dict)
        key = 'MOUNT_POINTS|{}'.format(mountpoint_dict[0][6])

        assert mountpoint_dict_return[key]['Filesystem'] == "udev"
        assert mountpoint_dict_return[key]['Type'] == "ext-4"
        assert mountpoint_dict_return[key]['1K-blocks'] == "10000"
        assert mountpoint_dict_return[key]['Used'] == "1000"
        assert mountpoint_dict_return[key]['Available'] == "9000"

    def test_create_mount_dict_empty(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        mountpoint_dict = [["udev", "tmpfs", "10000", "1000", "9000", "", "/dev2"]]
        mountpoint_dict_return = pdstatsd.create_mount_dict(mountpoint_dict)

        assert len(mountpoint_dict_return) == 0

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mount_dict", return_value={"MOUNT_POINTS|/dev": {"Filesystem": "udev", "Type": "ext-4", "1K-blocks": "10000", "Used": "1000", "Available": "9000"}}) 
    def test_update_mountpointstats_command(self, mock_create_mount_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_mountpointstats_command()
        mock_create_mount_dict.assert_called_once()
        assert ret == True 

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mount_dict", return_value={})
    def test_update_mountpointstats_command_empty(self, mock_create_mount_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_mountpointstats_command()
        mock_create_mount_dict.assert_called_once()
        assert not ret

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mount_dict", return_value={})
    @patch.object(procdockerstatsd.ProcDockerStats, "run_command", return_value={})
    def test_update_mountpointstats_command_fail(self, mock_create_mount_dict, mock_run_command):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_mountpointstats_command()

        mock_create_mount_dict.assert_called_once()
        assert ret == False

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mount_dict", return_value={"MOUNT_POINTS|/dev": {"Filesystem": "udev", "Type": "ext-4", "1K-blocks": "10000", "Used": "1000", "Available": "9000"}}) 
    def test_format_mount_cmd_output(self, mock_create_mount_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        in_lines = "Filesystem  Type  1K-blocks  Used  Available  Use%  Mounted on\nudev  ext-4  10000  1000  9000  0%  /dev"
        ret = pdstatsd.format_mount_cmd_output(in_lines)
        mock_create_mount_dict.assert_called_once()
        assert ret

    def test_create_mem_dict(self):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        mem_dict = {"MemTotal": "10000", "MemFree": "9000", "SwapTotal": "0", "SwapFree": "0", "Buffers": "123", "Cached": "456", "Shmem": "0"}
        mem_dict_return = pdstatsd.create_mem_dict(mem_dict)

        key = "MEMORY_STATS|Physical"
        assert int(mem_dict_return[key]['Used']) == 1000
        assert mem_dict_return[key]['1K-blocks'] == "10000"
        key = "MEMORY_STATS|Swap"
        assert int(mem_dict_return[key]['Used']) == 0
        assert mem_dict_return[key]['1K-blocks'] == "0"

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mem_dict", return_value={
        "MEMORY_STATS|Physical": {"1K-blocks": "10000", "Used": 1000}, 
        "MEMORY_STATS|Virtual": {"1K-blocks": "10000", "Used": 1000}, 
        "MEMORY_STATS|Buffer": {"1K-blocks": "10000", "Used": 123}, 
        "MEMORY_STATS|Cached": {"1K-blocks": "456", "Used": 456}, 
        "MEMORY_STATS|Shared": {"1K-blocks": "0", "Used": 0}, 
        "MEMORY_STATS|Swap": {"1K-blocks": "0", "Used": 0}})
    def test_format_mem_output(self, mock_create_mem_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)

        in_lines = "MemTotal: 10000 kB\n MemFree: 9000 kB\n SwapTotal: 0 kB\n SwapFree: 0\n Buffers: 123\n Cached: 456\n Shmem: 0"
        ret = pdstatsd.format_mem_output(in_lines)
        mock_create_mem_dict.assert_called_once()
        assert ret

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mem_dict", return_value={
        "MEMORY_STATS|Physical": {"1K-blocks": "10000", "Used": 1000}, 
        "MEMORY_STATS|Virtual": {"1K-blocks": "10000", "Used": 1000}, 
        "MEMORY_STATS|Buffer": {"1K-blocks": "10000", "Used": 123}, 
        "MEMORY_STATS|Cached": {"1K-blocks": "456", "Used": 456}, 
        "MEMORY_STATS|Shared": {"1K-blocks": "0", "Used": 0}, 
        "MEMORY_STATS|Swap": {"1K-blocks": "0", "Used": 0}})
    def test_update_memory_command(self, mock_create_mem_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_memory_command()
        mock_create_mem_dict.assert_called_once()
        assert ret == True 

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mem_dict", return_value={})
    def test_update_memory_command_empty(self, mock_create_mem_dict):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_memory_command()
        mock_create_mem_dict.assert_called_once()
        assert not ret

    @patch.object(procdockerstatsd.ProcDockerStats, "create_mem_dict", return_value={})
    @patch.object(procdockerstatsd.ProcDockerStats, "run_command", return_value={})
    def test_update_memory_command_fail(self, mock_create_mem_dict, mock_run_command):
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        ret = pdstatsd.update_memory_command()

        mock_create_mem_dict.assert_called_once()
        assert ret == False

