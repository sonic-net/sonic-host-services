import os
import sys
import pytest
import importlib.util
import importlib.machinery
from swsscommon import swsscommon
from unittest.mock import call, patch, Mock

from sonic_py_common.general import getstatusoutput_noshell
from tests.common.mock_configdb import MockConfigDb, MockDBConnector

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
sys.path.insert(0, modules_path)

# Load the file under test
hostcfgd_path = os.path.join(scripts_path, 'hostcfgd')
loader = importlib.machinery.SourceFileLoader('hostcfgd', hostcfgd_path)
spec = importlib.util.spec_from_loader(loader.name, loader)
hostcfgd = importlib.util.module_from_spec(spec)
loader.exec_module(hostcfgd)
sys.modules['hostcfgd'] = hostcfgd
original_syslog = hostcfgd.syslog

# Mock swsscommon classes
hostcfgd.ConfigDBConnector = MockConfigDb
hostcfgd.DBConnector = MockDBConnector
hostcfgd.Table = Mock()


AUDIT_INIT_CONFIG_FILE = "/etc/sonic/audit/security-auditing.rules"
AUDIT_CONFIG_FILE = "/etc/audit/rules.d/security-auditing.rules"
AUDIT_SYSLOG_CONFIG_FILE = "/etc/audit/plugins.d/syslog.conf"
RESTART_AUDITD = ['sudo', 'systemctl', 'restart', 'auditd']


class TestHostcfgdAudit(object):
    """
        Test hostcfd daemon - AUDIT
    """
    def setup_method(self):
        print("SETUP")
        self.test_data = {'enabled': 'true'}

    @patch('hostcfgd.subprocess.check_call')
    @patch('syslog.syslog')
    def test_audit_enable(self, mock_syslog, mock_subprocess):
        self.test_data['enabled'] = 'true'
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.audit_handler("config", "", self.test_data)

        expected_syslog = [
            call(hostcfgd.syslog.LOG_INFO, "Audit handler"),
            call(hostcfgd.syslog.LOG_INFO, "Audit configuration update")
        ]
        mock_syslog.assert_has_calls(expected_syslog)

        expected_subprocess = [
            call(['sudo', 'cp', AUDIT_INIT_CONFIG_FILE, AUDIT_CONFIG_FILE]),
            call(["sudo", "sed", "-i", "s/active = no/active = yes/", AUDIT_SYSLOG_CONFIG_FILE]),
            call(RESTART_AUDITD)
        ]
        mock_subprocess.assert_has_calls(expected_subprocess)

    @patch('hostcfgd.subprocess.check_call')
    @patch('syslog.syslog')
    def test_audit_disable(self, mock_syslog, mock_subprocess):
        self.test_data['enabled'] = 'false'
        print(self.test_data)
        MockConfigDb.set_config_db(self.test_data)
        host_config_daemon = hostcfgd.HostConfigDaemon()
        host_config_daemon.audit_handler("config", "", self.test_data)

        expected_syslog = [
            call(hostcfgd.syslog.LOG_INFO, "Audit handler"),
            call(hostcfgd.syslog.LOG_INFO, "Audit configuration update")
        ]
        mock_syslog.assert_has_calls(expected_syslog)

        expected_subprocess = [
            call(["sudo", "rm", AUDIT_CONFIG_FILE]),
            call(["sudo", "sed", "-i", "s/active = yes/active = no/", AUDIT_SYSLOG_CONFIG_FILE]),
            call(RESTART_AUDITD)
        ]
        mock_subprocess.assert_has_calls(expected_subprocess)

    def teardown_method(self):
        print("TEARDOWN")
