import importlib.machinery
import importlib.util
import filecmp
import shutil
import os
import sys
import subprocess
import re

from parameterized import parameterized
from unittest import TestCase, mock
from tests.hostcfgd.test_ssh_server_vectors import HOSTCFGD_TEST_SSH_SERVER_VECTOR
from tests.common.mock_configdb import MockConfigDb, MockDBConnector

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, "scripts")
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


class TestHostcfgdSSHServer(TestCase):
    """
        Test hostcfd daemon - SSHServer
    """
    def run_diff(self, file1, file2):
        try:
            diff_out = subprocess.check_output('diff -ur {} {} || true'.format(file1, file2), shell=True)
            return diff_out
        except subprocess.CalledProcessError as err:
            syslog.syslog(syslog.LOG_ERR, "{} - failed: return code - {}, output:\n{}".format(err.cmd, err.returncode, err.output))
            return -1

    """
        Check different config
    """
    def check_config(self, test_name, test_data, config_name):
        op_path = output_path + "/" + test_name + "_" + config_name
        sop_path = sample_output_path + "/" +  test_name + "_" + config_name
        sop_path_common = sample_output_path + "/" +  test_name
        hostcfgd.SSH_CONFG = op_path + "/sshd_config"
        hostcfgd.SSH_CONFG_TMP = hostcfgd.SSH_CONFG + ".tmp"
        shutil.rmtree(op_path, ignore_errors=True)
        os.mkdir(op_path)

        shutil.copyfile(sop_path_common + "/sshd_config.old", op_path + "/sshd_config")
        MockConfigDb.set_config_db(test_data[config_name])
        host_config_daemon = hostcfgd.HostConfigDaemon()

        try:
            ssh_table = host_config_daemon.config_db.get_table('SSH_SERVER')
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "failed: get_table 'SSH_SERVER', exception={}".format(e))
            ssh_table = []

        host_config_daemon.sshscfg.load(ssh_table)


        diff_output = ""
        files_to_compare = ['sshd_config']

        # check output files exists
        for name in files_to_compare:
            if not os.path.isfile(sop_path + "/" + name):
                raise ValueError('filename: %s not exit' % (sop_path + "/" + name))
            if not os.path.isfile(op_path + "/" + name):
                raise ValueError('filename: %s not exit' % (op_path + "/" + name))

        # deep comparison
        match, mismatch, errors = filecmp.cmpfiles(sop_path, op_path, files_to_compare, shallow=False)

        if not match:
            for name in files_to_compare:
                diff_output += self.run_diff( sop_path + "/" + name,\
                    op_path + "/" + name).decode('utf-8')

        self.assertTrue(len(diff_output) == 0, diff_output)

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_default_values(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "default_values")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_login_timeout(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_login_timeout")


    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_authentication_retries(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_authentication_retries")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_ports(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_ports")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_password_authentication(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_password_authentication")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_permit_root_login(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_permit_root_login")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_ciphers(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_ciphers")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_kex_algorithms(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_kex_algorithms")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_macs(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_macs")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_VECTOR)
    def test_hostcfgd_sshs_all(self, test_name, test_data):
        """
            Test SSHS hostcfd daemon initialization

            Args:
                test_name(str): test name
                test_data(dict): test data which contains initial Config Db tables, and expected results

            Returns:
                None
        """

        self.check_config(test_name, test_data, "modify_all")
