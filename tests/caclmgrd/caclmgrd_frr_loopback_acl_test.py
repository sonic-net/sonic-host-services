import os
import sys

from swsscommon import swsscommon
from parameterized import parameterized
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs
from sonic_py_common.general import load_module_from_source

from .test_frr_loopback_acl_vectors import CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR
from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'


def _accept_rule(port):
    return (
        'iptables', '-A', 'OUTPUT', '-o', 'lo',
        '-p', 'tcp', '--dport', str(port),
        '-m', 'owner', '--uid-owner', '300',
        '-j', 'ACCEPT',
    )


def _drop_rule(port):
    return (
        'iptables', '-A', 'OUTPUT', '-o', 'lo',
        '-p', 'tcp', '--dport', str(port),
        '-j', 'DROP',
    )


class TestCaclmgrdFrrLoopbackAcl(TestCase):
    """
    Verify caclmgrd installs OUTPUT-chain rules that restrict FRR daemon TCP
    ports on the loopback interface to the FRR user (UID 300).
    """

    EXPECTED_PORTS = (2601, 2620)

    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    def _build_daemon(self):
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        return self.caclmgrd.ControlPlaneAclManager("caclmgrd")

    @parameterized.expand(CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR)
    @patchfs
    def test_frr_loopback_rules_are_generated(self, _test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        caclmgrd_daemon = self._build_daemon()

        rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
            '', MockConfigDb())
        rules_ret = [tuple(r) for r in rules_ret]

        for port in self.EXPECTED_PORTS:
            self.assertIn(_accept_rule(port), rules_ret,
                          "Missing ACCEPT rule for FRR port {}".format(port))
            self.assertIn(_drop_rule(port), rules_ret,
                          "Missing DROP rule for FRR port {}".format(port))

    @parameterized.expand(CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR)
    @patchfs
    def test_accept_precedes_drop_per_port(self, _test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        caclmgrd_daemon = self._build_daemon()

        rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
            '', MockConfigDb())
        rules_ret = [tuple(r) for r in rules_ret]

        for port in self.EXPECTED_PORTS:
            accept_idx = rules_ret.index(_accept_rule(port))
            drop_idx = rules_ret.index(_drop_rule(port))
            self.assertLess(
                accept_idx, drop_idx,
                "ACCEPT (uid-owner) must precede DROP for port {}".format(port))

    @parameterized.expand(CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR)
    @patchfs
    def test_no_generic_output_drop_precedes_frr_accept(self, _test_name, test_data, fs):
        """
        Guard against a regression where some other helper appends an
        IPv4 OUTPUT-chain DROP/REJECT/jump above our ACCEPT rules. Such a
        rule would silently shadow the per-UID ACCEPT and break FRR.
        Only DROPs/REJECTs/jumps to bespoke chains targeting our specific
        ports are an issue here, but we conservatively assert no such
        terminating verdict appears in the OUTPUT chain anywhere before
        our first ACCEPT.
        """
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        caclmgrd_daemon = self._build_daemon()

        rules_ret, _ = caclmgrd_daemon.get_acl_rules_and_translate_to_iptables_commands(
            '', MockConfigDb())
        rules_ret = [tuple(r) for r in rules_ret]

        first_frr_accept_idx = min(
            rules_ret.index(_accept_rule(p)) for p in self.EXPECTED_PORTS)

        terminating_verdicts = {'DROP', 'REJECT'}
        for idx, cmd in enumerate(rules_ret[:first_frr_accept_idx]):
            if 'iptables' not in cmd or '-A' not in cmd:
                continue
            try:
                chain_pos = cmd.index('-A') + 1
            except ValueError:
                continue
            if chain_pos >= len(cmd) or cmd[chain_pos] != 'OUTPUT':
                continue
            if '-j' in cmd:
                target = cmd[cmd.index('-j') + 1]
                self.assertNotIn(
                    target, terminating_verdicts,
                    "Found IPv4 OUTPUT-chain {} rule at index {} which precedes "
                    "the FRR ACCEPT rule and could shadow it: {}".format(
                        target, idx, ' '.join(cmd)))

    @parameterized.expand(CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR)
    @patchfs
    def test_helper_emits_rules_in_non_default_namespace(self, _test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        caclmgrd_daemon = self._build_daemon()

        ns = 'asic0'
        ns_prefix = ['ip', 'netns', 'exec', ns]
        caclmgrd_daemon.iptables_cmd_ns_prefix[ns] = ns_prefix

        cmds = caclmgrd_daemon.generate_frr_daemon_loopback_acl_commands(ns)

        self.assertEqual(
            len(cmds),
            2 * len(caclmgrd_daemon.FRR_LOOPBACK_TCP_DROP_PORTS),
            "Expected one ACCEPT+DROP pair per configured FRR port in ASIC namespace")

        for cmd in cmds:
            self.assertEqual(
                cmd[:len(ns_prefix)], ns_prefix,
                "ASIC-namespace command must start with `ip netns exec <ns>` prefix")

        for port in self.EXPECTED_PORTS:
            self.assertIn(tuple(ns_prefix) + _accept_rule(port), [tuple(c) for c in cmds])
            self.assertIn(tuple(ns_prefix) + _drop_rule(port), [tuple(c) for c in cmds])

    @parameterized.expand(CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR)
    @patchfs
    def test_helper_emits_one_pair_per_configured_port(self, _test_name, test_data, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        MockConfigDb.set_config_db(test_data["config_db"])
        caclmgrd_daemon = self._build_daemon()

        cmds = caclmgrd_daemon.generate_frr_daemon_loopback_acl_commands('')
        self.assertEqual(
            len(cmds),
            2 * len(caclmgrd_daemon.FRR_LOOPBACK_TCP_DROP_PORTS),
            "Expected exactly one ACCEPT+DROP pair per configured FRR port")
