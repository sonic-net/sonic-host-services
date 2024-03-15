import importlib.machinery
import importlib.util
import os
import sys

from copy import copy
from swsscommon import swsscommon
from syslog import syslog, LOG_ERR
from tests.hostcfgd.test_logging_vectors \
    import HOSTCFGD_TEST_LOGGING_VECTOR as logging_test_data
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


class TestHostcfgLogging(TestCase):
    """
        Test hostcfgd daemon - LogRotate
    """

    def __init__(self, *args, **kwargs):
        super(TestHostcfgLogging, self).__init__(*args, **kwargs)
        self.host_config_daemon = None

    def setUp(self):
        MockConfigDb.set_config_db(logging_test_data['initial'])
        self.host_config_daemon = hostcfgd.HostConfigDaemon()

        logging_config = self.host_config_daemon.config_db.get_table(
            swsscommon.CFG_LOGGING_TABLE_NAME)

        assert self.host_config_daemon.loggingcfg.cache == {}
        self.host_config_daemon.loggingcfg.load(logging_config)
        assert self.host_config_daemon.loggingcfg.cache != {}

        # Reset run_cmd mock
        hostcfgd.run_cmd.reset_mock()

    def tearDown(self):
        self.host_config_daemon = None
        MockConfigDb.set_config_db({})

    def update_config(self, config_name):
        MockConfigDb.mod_config_db(logging_test_data[config_name])

        syslog_data = logging_test_data[config_name]['LOGGING']['syslog']
        debug_data = logging_test_data[config_name]['LOGGING']['debug']

        self.host_config_daemon.logging_handler(key='syslog', op=None,
                                                data=syslog_data)
        self.host_config_daemon.logging_handler(key='debug', op=None,
                                                data=debug_data)

    def assert_applied(self, config_name):
        """Assert that updated config triggered appropriate services

        Args:
            config_name: str:   Test vectors config name

        Assert:
            Assert when config wasn't used
        """
        orig_cache = copy(self.host_config_daemon.loggingcfg.cache)
        self.update_config(config_name)
        assert self.host_config_daemon.loggingcfg.cache != orig_cache
        hostcfgd.run_cmd.assert_called()

    def test_rsyslog_handle_modified(self):
        self.assert_applied('modified')
