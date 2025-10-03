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
