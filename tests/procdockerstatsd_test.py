import sys
import os
import pytest
from unittest.mock import call, patch
from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source

from .mock_connector import MockConnector

swsscommon.SonicV2Connector = MockConnector

test_path = os.path.dirname(os.path.abspath(__file__))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
procdockerstatsd_path = os.path.join(scripts_path, 'procdockerstatsd')
procdockerstatsd = load_module_from_source('procdockerstatsd', procdockerstatsd_path)

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
        expected_calls = [call(["ps", "-eo", "uid,pid,ppid,%mem,%cpu,stime,tty,time,cmd", "--sort", "-%cpu"], ["head", "-1024"])]
        pdstatsd = procdockerstatsd.ProcDockerStats(procdockerstatsd.SYSLOG_IDENTIFIER)
        with patch("procdockerstatsd.getstatusoutput_noshell_pipe", return_value=([0, 0], 'output')) as mock_cmd:
            pdstatsd.update_processstats_command()
            mock_cmd.assert_has_calls(expected_calls)

