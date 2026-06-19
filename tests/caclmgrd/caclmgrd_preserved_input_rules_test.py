import os
import sys

from pyfakefs.fake_filesystem_unittest import patchfs
from sonic_py_common.general import load_module_from_source
from swsscommon import swsscommon
from unittest import TestCase, mock

from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

# A representative `iptables -S INPUT` snapshot: the dhcp_server container's
# commented docker0 syslog rule (externally owned), a rule with a different
# comment, an uncommented caclmgrd-style rule, and the policy line.
SAMPLE_INPUT_DUMP = "\n".join([
    "-P INPUT ACCEPT",
    "-A INPUT -i docker0 -p udp -m udp --dport 514 -m comment --comment dhcp_server_syslog -j ACCEPT",
    "-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT",
    "-A INPUT -i docker0 -p udp --dport 9999 -m comment --comment some_other_service -j ACCEPT",
    "-A INPUT -j DROP",
])

DHCP_SYSLOG_RULE = (
    'iptables', '-A', 'INPUT', '-i', 'docker0', '-p', 'udp', '-m', 'udp',
    '--dport', '514', '-m', 'comment', '--comment', 'dhcp_server_syslog', '-j', 'ACCEPT'
)


class TestCaclmgrdPreservedInputRules(TestCase):
    """
        caclmgrd owns the host INPUT chain and flushes/rebuilds it on every CACL
        change. Rules owned by OTHER services (tagged with an iptables --comment in
        IGNORED_INPUT_RULE_COMMENTS, e.g. the dhcp_server container's docker0 syslog
        rule) must be preserved across that rebuild and re-added before any DROP.
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    def setup_daemon(self, config_db):
        MockConfigDb.set_config_db(config_db)
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock()
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.generate_allow_internal_docker_ip_traffic_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.generate_allow_internal_chasis_midplane_traffic = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        return self.caclmgrd.ControlPlaneAclManager("caclmgrd")

    @patchfs
    def test_snapshot_keeps_only_ignore_listed_comments(self, fs):
        """Only rules whose --comment is in IGNORED_INPUT_RULE_COMMENTS are kept;
        a different comment and uncommented/policy lines are skipped. The kept
        rule is reproduced verbatim as an 'iptables -A INPUT ...' command."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        daemon.iptables_cmd_ns_prefix[''] = []

        with mock.patch.object(self.caclmgrd.subprocess, 'check_output', return_value=SAMPLE_INPUT_DUMP):
            preserved = daemon.get_preserved_input_rules('')

        preserved = [tuple(r) for r in preserved]
        self.assertEqual(preserved, [DHCP_SYSLOG_RULE])

    @patchfs
    def test_snapshot_handles_quoted_comment_with_spaces(self, fs):
        """iptables -S quotes comments containing spaces; shlex parsing must keep
        the comment as one unquoted token so the re-add stays byte-identical and
        the owner's -C/-D still match (no literal quote chars)."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        daemon.iptables_cmd_ns_prefix[''] = []
        daemon.IGNORED_INPUT_RULE_COMMENTS = ['multi word comment']
        dump = '-A INPUT -i docker0 -p udp -m udp --dport 514 ' \
               '-m comment --comment "multi word comment" -j ACCEPT'

        with mock.patch.object(self.caclmgrd.subprocess, 'check_output', return_value=dump):
            preserved = daemon.get_preserved_input_rules('')

        self.assertEqual(len(preserved), 1)
        self.assertIn('multi word comment', preserved[0])  # single unquoted token
        self.assertNotIn('"multi', preserved[0])           # no literal quote chars

    @patchfs
    def test_snapshot_returns_empty_on_iptables_failure(self, fs):
        """If the chain can't be read, preservation degrades gracefully to []."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        daemon.iptables_cmd_ns_prefix[''] = []

        with mock.patch.object(self.caclmgrd.subprocess, 'check_output', side_effect=OSError("no iptables")):
            self.assertEqual(daemon.get_preserved_input_rules(''), [])

    @patchfs
    def test_preserved_rule_reinserted_before_catch_all_drop(self, fs):
        """End-to-end: with a CACL rule present (so the catch-all DROP exists), the
        preserved rule appears in the rebuilt chain AND strictly before the DROP,
        guaranteeing no flush-window where a DROP exists without it."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        config_db = {
            "ACL_TABLE": {
                "SSH_ONLY": {"stage": "INGRESS", "type": "CTRLPLANE", "services": ["SSH"]},
            },
            "ACL_RULE": {
                "SSH_ONLY|RULE_1": {"PACKET_ACTION": "ACCEPT", "PRIORITY": "9999", "SRC_IP": "10.0.0.0/8"},
            },
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {},
        }
        daemon = self.setup_daemon(config_db)
        daemon.iptables_cmd_ns_prefix[''] = []

        with mock.patch.object(daemon, 'get_preserved_input_rules', return_value=[list(DHCP_SYSLOG_RULE)]):
            cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())

        cmds = [tuple(c) for c in cmds]
        catch_all_drop = ('iptables', '-A', 'INPUT', '-j', 'DROP')
        self.assertIn(DHCP_SYSLOG_RULE, cmds, "preserved rule must be present in rebuild")
        self.assertIn(catch_all_drop, cmds, "test setup should produce a catch-all DROP")
        self.assertLess(cmds.index(DHCP_SYSLOG_RULE), cmds.index(catch_all_drop),
                        "preserved ACCEPT must come before the catch-all DROP")

    @patchfs
    def test_preserve_skipped_for_non_host_namespace(self, fs):
        """The docker0 rule is host/IPv4 only; ASIC namespaces must not even probe
        the chain (snapshot not invoked)."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        daemon.iptables_cmd_ns_prefix['asic0'] = []

        with mock.patch.object(daemon, 'get_preserved_input_rules', return_value=[list(DHCP_SYSLOG_RULE)]) as snap:
            cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('asic0', MockConfigDb())

        snap.assert_not_called()
        self.assertNotIn(DHCP_SYSLOG_RULE, [tuple(c) for c in cmds])
