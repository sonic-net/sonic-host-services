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
from tests.hostcfgd.test_ssh_server_vectors import HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR
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


class SshServerCheckConfigMixin(object):
    """
        Shared check_config()/run_diff() helpers for hostcfgd SSHServer
        tests. Kept as a plain mixin (not a TestCase) so that classes
        reusing it don't also inherit - and therefore re-collect and
        re-run - each other's @parameterized.expand test methods.
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


class TestHostcfgdSSHServer(SshServerCheckConfigMixin, TestCase):
    """
        Test hostcfd daemon - SSHServer
    """
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


class TestHostcfgdSSHServerListenAddresses(SshServerCheckConfigMixin, TestCase):
    """
        Test hostcfgd daemon - SSHServer listen_addresses (ADO 29390131)

        get_dut_ip_addresses() is patched so that 10.0.0.1, 10.0.0.2,
        fe80::1 and fe80::2 are treated as currently assigned to the DUT,
        without depending on this test host's real network interfaces.

        Uses SshServerCheckConfigMixin (not TestHostcfgdSSHServer) so that
        the existing ports/ciphers/kex/macs/etc. parameterized tests are
        not re-collected and re-run under this class as well.
    """
    ASSIGNED_ADDRESSES = frozenset(["10.0.0.1", "10.0.0.2", "fe80::1", "fe80::2"])

    def setUp(self):
        self._orig_get_dut_ip_addresses = hostcfgd.get_dut_ip_addresses
        hostcfgd.get_dut_ip_addresses = mock.Mock(return_value=set(self.ASSIGNED_ADDRESSES))

    def tearDown(self):
        hostcfgd.get_dut_ip_addresses = self._orig_get_dut_ip_addresses

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR)
    def test_hostcfgd_sshs_listen_addresses_default_values(self, test_name, test_data):
        """ listen_addresses absent - retains existing (untouched) ListenAddress lines """
        self.check_config(test_name, test_data, "default_values")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR)
    def test_hostcfgd_sshs_listen_addresses_ipv4(self, test_name, test_data):
        """ One assigned IPv4 listener """
        self.check_config(test_name, test_data, "listen_ipv4")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR)
    def test_hostcfgd_sshs_listen_addresses_ipv6(self, test_name, test_data):
        """ One assigned IPv6 listener """
        self.check_config(test_name, test_data, "listen_ipv6")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR)
    def test_hostcfgd_sshs_listen_addresses_ipv4_ipv6(self, test_name, test_data):
        """ Combined assigned IPv4 and IPv6 listeners """
        self.check_config(test_name, test_data, "listen_ipv4_ipv6")

    @parameterized.expand(HOSTCFGD_TEST_SSH_SERVER_LISTEN_ADDRESSES_VECTOR)
    def test_hostcfgd_sshs_listen_addresses_multiple(self, test_name, test_data):
        """ Multiple assigned addresses """
        self.check_config(test_name, test_data, "listen_multiple")

    def _make_ssh_server(self, config_dir):
        """ Build an SshServer with sshd_config seeded from the shared
            listen_addresses baseline (commented wildcards), pointed at a
            fresh, isolated config_dir. """
        os.makedirs(config_dir, exist_ok=True)
        shutil.copyfile(
            sample_output_path + "/SSH_SERVER_LISTEN_ADDRESSES/sshd_config.old",
            config_dir + "/sshd_config")
        hostcfgd.SSH_CONFG = config_dir + "/sshd_config"
        hostcfgd.SSH_CONFG_TMP = hostcfgd.SSH_CONFG + ".tmp"
        return hostcfgd.SshServer()

    def _listen_address_lines(self, path):
        with open(path) as f:
            return [line.strip() for line in f if 'ListenAddress' in line]

    def test_listen_addresses_replaces_existing_explicit_addresses(self):
        """ Re-configuring listen_addresses removes the previous explicit
            addresses and renders only the new set. """
        config_dir = output_path + "/listen_addresses_replace"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["10.0.0.1"]})
        lines = self._listen_address_lines(hostcfgd.SSH_CONFG)
        self.assertEqual(lines, ["ListenAddress 10.0.0.1"])

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["10.0.0.2", "fe80::2"]})
        lines = self._listen_address_lines(hostcfgd.SSH_CONFG)
        self.assertNotIn("ListenAddress 10.0.0.1", lines)
        self.assertEqual(set(lines), {"ListenAddress 10.0.0.2", "ListenAddress fe80::2"})

    def test_listen_addresses_removed_restores_wildcards(self):
        """ Removing only listen_addresses (other policies remain) restores
            both IPv4/IPv6 wildcard listeners. """
        config_dir = output_path + "/listen_addresses_removed"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)

        ssh_server.policies_update('POLICIES', {"ports": "22", "listen_addresses": ["10.0.0.1"]})
        self.assertEqual(self._listen_address_lines(hostcfgd.SSH_CONFG), ["ListenAddress 10.0.0.1"])

        # listen_addresses is now absent, but other policies remain
        ssh_server.policies_update('POLICIES', {"ports": "22"})
        lines = self._listen_address_lines(hostcfgd.SSH_CONFG)
        self.assertEqual(set(lines), {"ListenAddress 0.0.0.0", "ListenAddress ::"})

    def test_listen_addresses_row_deleted_restores_wildcards(self):
        """ Deleting the whole SSH_SERVER|POLICIES row restores both
            IPv4/IPv6 wildcard listeners. """
        config_dir = output_path + "/listen_addresses_row_deleted"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["fe80::1"]})
        self.assertEqual(self._listen_address_lines(hostcfgd.SSH_CONFG), ["ListenAddress fe80::1"])

        # Whole row deleted: CONFIG_DB delivers empty data
        ssh_server.policies_update('POLICIES', {})
        lines = self._listen_address_lines(hostcfgd.SSH_CONFG)
        self.assertEqual(set(lines), {"ListenAddress 0.0.0.0", "ListenAddress ::"})
        self.assertEqual(ssh_server.policies, {})

    def test_listen_addresses_absent_preserves_wildcard_behavior(self):
        """ listen_addresses never configured - existing ListenAddress
            lines (whatever they were) are left untouched. """
        config_dir = output_path + "/listen_addresses_never_configured"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        before = self._listen_address_lines(hostcfgd.SSH_CONFG)

        ssh_server.policies_update('POLICIES', {"ports": "22"})
        after = self._listen_address_lines(hostcfgd.SSH_CONFG)
        self.assertEqual(before, after)

    def test_listen_addresses_invalid_value_rejected(self):
        """ A malformed address rejects the whole listener update, removes
            the tmp file, and leaves the active sshd_config unchanged. """
        config_dir = output_path + "/listen_addresses_invalid"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            before = f.read()

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["10.0.0.999"]})

        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(hostcfgd.SSH_CONFG_TMP))

    def test_listen_addresses_unassigned_value_rejected(self):
        """ A syntactically valid but unassigned address is rejected, and
            the active sshd_config is left unchanged. """
        config_dir = output_path + "/listen_addresses_unassigned"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            before = f.read()

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["192.0.2.55"]})

        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(hostcfgd.SSH_CONFG_TMP))

    def test_listen_addresses_one_unassigned_rejects_whole_update(self):
        """ If one address in a multi-address list is unassigned, the
            complete listener update is rejected (none of the addresses,
            including the valid/assigned ones, get applied). """
        config_dir = output_path + "/listen_addresses_partial_invalid"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            before = f.read()

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["10.0.0.1", "192.0.2.55"]})

        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertNotIn("ListenAddress 10.0.0.1", self._listen_address_lines(hostcfgd.SSH_CONFG))

    def test_listen_addresses_duplicate_rejected(self):
        """ Duplicate addresses are rejected deterministically (rather than
            silently de-duplicated), and the active config is unchanged. """
        config_dir = output_path + "/listen_addresses_duplicate"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            before = f.read()

        ssh_server.policies_update('POLICIES', {"listen_addresses": ["10.0.0.1", "10.0.0.1"]})

        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(hostcfgd.SSH_CONFG_TMP))

    def test_generated_config_failing_sshd_dash_t(self):
        """ If sshd -T rejects the generated config (e.g. bad cipher name),
            the tmp file is removed and the active config is unchanged -
            even though listen_addresses itself was valid. """
        config_dir = output_path + "/listen_addresses_sshd_t_failure"
        shutil.rmtree(config_dir, ignore_errors=True)
        ssh_server = self._make_ssh_server(config_dir)
        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            before = f.read()

        ssh_server.policies_update('POLICIES', {
            "listen_addresses": ["10.0.0.1"],
            "ciphers": ["not-a-real-cipher"]
        })

        with open(hostcfgd.SSH_CONFG, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(hostcfgd.SSH_CONFG_TMP))
