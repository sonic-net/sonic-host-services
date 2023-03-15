import importlib.machinery
import importlib.util
import os
import sys

from copy import copy
from parameterized import parameterized
from swsscommon import swsscommon
from syslog import syslog, LOG_ERR
from tests.hostcfgd.test_rsyslog_vectors \
    import HOSTCFGD_TEST_RSYSLOG_VECTOR as rsyslog_test_data
from tests.common.mock_configdb import MockConfigDb, MockDBConnector
from unittest import TestCase, mock

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
src_path = os.path.dirname(modules_path)
templates_path = os.path.join(src_path, "sonic-host-services-data/templates")
output_path = os.path.join(test_path, "hostcfgd/output")
sample_output_path = os.path.join(test_path, "hostcfgd/sample_output")
sys.path.insert(0, modules_path)

# Load the file under test
hostcfgd_path = os.path.join(scripts_path, 'hostcfgd')
loader = importlib.machinery.SourceFileLoader('hostcfgd', hostcfgd_path)
spec = importlib.util.spec_from_loader(loader.name, loader)
hostcfgd = importlib.util.module_from_spec(spec)
loader.exec_module(hostcfgd)
sys.modules['hostcfgd'] = hostcfgd

# Mock swsscommon classes
hostcfgd.ConfigDBConnector = MockConfigDb
hostcfgd.DBConnector = MockDBConnector
hostcfgd.Table = mock.Mock()
hostcfgd.run_cmd = mock.Mock()


class TestHostcfgdRSyslog(TestCase):
    """
        Test hostcfgd daemon - RSyslog
    """

    def __init__(self, *args, **kwargs):
        super(TestHostcfgdRSyslog, self).__init__(*args, **kwargs)
        self.host_config_daemon = None

    def setUp(self):
        MockConfigDb.set_config_db(rsyslog_test_data['initial'])
        self.host_config_daemon = hostcfgd.HostConfigDaemon()

        syslog_config = self.host_config_daemon.config_db.get_table(
            swsscommon.CFG_SYSLOG_CONFIG_TABLE_NAME)
        syslog_server = self.host_config_daemon.config_db.get_table(
            swsscommon.CFG_SYSLOG_SERVER_TABLE_NAME)

        assert self.host_config_daemon.rsyslogcfg.cache == {}
        self.host_config_daemon.rsyslogcfg.load(syslog_config, syslog_server)
        assert self.host_config_daemon.rsyslogcfg.cache != {}

        # Reset run_cmd mock
        hostcfgd.run_cmd.reset_mock()

    def tearDown(self):
        self.host_config_daemon = None
        MockConfigDb.set_config_db({})

    def update_config(self, config_name):
        MockConfigDb.mod_config_db(rsyslog_test_data[config_name])
        self.host_config_daemon.rsyslog_config_handler(None, None, None)

    def assert_applied(self, config_name):
        """Assert that updated config triggered appropriate services

        Args:
            config_name: str:   Test vectors config name

        Assert:
            Assert when config wasn't used
        """
        orig_cache = copy(self.host_config_daemon.rsyslogcfg.cache)
        self.update_config(config_name)
        assert self.host_config_daemon.rsyslogcfg.cache != orig_cache
        hostcfgd.run_cmd.assert_called()

    def assert_not_applied(self, config_name):
        """Assert that the same config does not affect on services

        Args:
            config_name: str:   Test vectors config name

        Assert:
            Assert when config was used
        """
        orig_cache = copy(self.host_config_daemon.rsyslogcfg.cache)
        self.update_config(config_name)
        assert self.host_config_daemon.rsyslogcfg.cache == orig_cache
        hostcfgd.run_cmd.assert_not_called()

    def test_rsyslog_handle_change_global(self):
        self.assert_applied('change_global')

    def test_rsyslog_handle_change_server(self):
        self.assert_applied('change_server')

    def test_rsyslog_handle_add_server(self):
        self.assert_applied('add_server')

    def test_rsyslog_handle_empty(self):
        self.assert_applied('empty_config')

    def test_rsyslog_handle_the_same_config(self):
        self.assert_not_applied('initial')
