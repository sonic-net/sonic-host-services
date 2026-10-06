import os
import sys

from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

BRIDGE_SUBNET = '240.127.1.0/24'

FORWARD_OUT_RULE = (
    'iptables', '-A', 'FORWARD', '-i', 'docker0', '!', '-o', 'docker0',
    '-j', 'ACCEPT', '-m', 'comment', '--comment', 'redfish_egress',
)
FORWARD_REPLY_RULE = (
    'iptables', '-A', 'FORWARD', '-o', 'docker0', '-m', 'conntrack',
    '--ctstate', 'RELATED,ESTABLISHED', '-j', 'ACCEPT',
    '-m', 'comment', '--comment', 'redfish_egress',
)
NAT_RULE_ARGS = [
    '-s', BRIDGE_SUBNET, '!', '-o', 'docker0', '-j', 'MASQUERADE',
    '-m', 'comment', '--comment', 'redfish_egress',
]
MGMT_DROP_RULE = (
    'iptables', '-A', 'FORWARD', '-i', 'eth0', '-j', 'DROP',
    '-m', 'comment', '--comment', 'redfish_egress',
)
NAT_ADD = tuple(['iptables', '-t', 'nat', '-A', 'POSTROUTING'] + NAT_RULE_ARGS)
NAT_DEL = tuple(['iptables', '-t', 'nat', '-D', 'POSTROUTING'] + NAT_RULE_ARGS)


class TestCaclmgrdRedfishEgress(TestCase):
    """
        Verifies caclmgrd owns the rules that let bmcweb originate connections
        from the bridge network to subscribers on the management network.

        dockerd runs with --iptables=false, so the masquerade docker would
        normally install is absent and event deliveries leave the management
        interface with an unroutable bridge-subnet source. The forward path is
        scoped to what the feature needs: out from the bridge, and replies to
        those connections back in.
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)
        self.maxDiff = None

    def setup_daemon(self, config_db):
        MockConfigDb.set_config_db(config_db)
        # real strings: the walk joins every command for logging, so a bare
        # MagicMock here fails before reaching the assertions
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ip = mock.MagicMock(return_value='10.0.0.1')
        self.caclmgrd.ControlPlaneAclManager.get_namespace_mgmt_ipv6 = mock.MagicMock(return_value='fd00::1')
        self.caclmgrd.ControlPlaneAclManager.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.generate_allow_internal_docker_ip_traffic_commands = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.generate_allow_internal_chasis_midplane_traffic = mock.MagicMock(return_value=[])
        self.caclmgrd.ControlPlaneAclManager.get_chain_list = mock.MagicMock(return_value=["INPUT", "FORWARD", "OUTPUT"])
        self.caclmgrd.ControlPlaneAclManager.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        return self.caclmgrd.ControlPlaneAclManager("caclmgrd")

    def enabled_daemon(self):
        return self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                  "FEATURE": {"redfish": {"state": "enabled"}}})

    def disabled_daemon(self):
        return self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                  "FEATURE": {"redfish": {"state": "disabled"}}})

    # --- the FORWARD pair, rebuilt by every ruleset walk --------------------

    @patchfs
    def test_forward_pair_emitted_when_feature_enabled(self, fs):
        """Both directions are programmed when the feature is on."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        self.assertIn(FORWARD_OUT_RULE, cmds)
        self.assertIn(FORWARD_REPLY_RULE, cmds)

    @patchfs
    def test_forward_pair_absent_when_feature_disabled(self, fs):
        """Nothing is programmed when the feature is off."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.disabled_daemon()
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        self.assertNotIn(FORWARD_OUT_RULE, cmds)
        self.assertNotIn(FORWARD_REPLY_RULE, cmds)

    @patchfs
    def test_forward_pair_absent_when_feature_entry_missing(self, fs):
        """Images built without redfish have no FEATURE entry at all."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        self.assertNotIn(FORWARD_OUT_RULE, cmds)
        self.assertNotIn(FORWARD_REPLY_RULE, cmds)

    @patchfs
    def test_forward_pair_host_namespace_only(self, fs):
        """docker0 lives in the host namespace, so asic namespaces get nothing."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.iptables_cmd_ns_prefix['asic0'] = []
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('asic0', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        self.assertNotIn(FORWARD_OUT_RULE, cmds)
        self.assertNotIn(FORWARD_REPLY_RULE, cmds)

    @patchfs
    def test_mgmt_ingress_dropped_after_the_reply_rule(self, fs):
        """Enabling forwarding on the management interface would otherwise let
        the host forward anything arriving there. The terminal drop closes it,
        and must come after the established-reply accept or replies die too."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        self.assertIn(MGMT_DROP_RULE, cmds)
        self.assertLess(cmds.index(FORWARD_REPLY_RULE), cmds.index(MGMT_DROP_RULE),
                        "the established-reply accept must precede the drop")
        self.assertEqual(cmds.index(MGMT_DROP_RULE), len(cmds) - 1,
                         "the drop is terminal, so nothing may follow it")

    @patchfs
    def test_mgmt_ingress_drop_absent_when_disabled(self, fs):
        """No forwarding is enabled when the feature is off, so nothing to close."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.disabled_daemon()
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertNotIn(MGMT_DROP_RULE, [tuple(c) for c in cmds])

    # --- the masquerade, programmed on transitions and reconciled on walks --

    @patchfs
    def test_nat_added_only_when_absent(self, fs):
        """An add happens when the rule is missing, and is skipped when present,
        so repeated daemon restarts cannot stack duplicates."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.get_bridge_subnet = mock.MagicMock(return_value=BRIDGE_SUBNET)
        daemon.run_commands = mock.MagicMock()

        daemon.redfish_egress_nat_present = mock.MagicMock(return_value=False)
        daemon.program_redfish_egress_nat(True)
        self.assertEqual([tuple(c) for c in daemon.run_commands.call_args[0][0]], [NAT_ADD])

        daemon.run_commands.reset_mock()
        daemon.redfish_egress_nat_present = mock.MagicMock(return_value=True)
        daemon.program_redfish_egress_nat(True)
        daemon.run_commands.assert_not_called()

    @patchfs
    def test_nat_removal_clears_duplicates(self, fs):
        """Removal repeats while the rule is still present, so duplicates from
        any source are cleared, and stops once it is gone."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.get_bridge_subnet = mock.MagicMock(return_value=BRIDGE_SUBNET)
        daemon.run_commands = mock.MagicMock()
        # present, present, then gone
        daemon.redfish_egress_nat_present = mock.MagicMock(side_effect=[True, True, False])

        daemon.program_redfish_egress_nat(False)

        issued = [tuple(c[0][0][0]) for c in daemon.run_commands.call_args_list]
        self.assertEqual(issued, [NAT_DEL, NAT_DEL])

    @patchfs
    def test_nat_removal_is_bounded(self, fs):
        """A delete that never takes effect must not spin forever."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.get_bridge_subnet = mock.MagicMock(return_value=BRIDGE_SUBNET)
        daemon.run_commands = mock.MagicMock()
        daemon.redfish_egress_nat_present = mock.MagicMock(return_value=True)

        daemon.program_redfish_egress_nat(False)

        self.assertEqual(daemon.run_commands.call_count,
                         daemon.REDFISH_EGRESS_NAT_MAX_DUPLICATES)

    @patchfs
    def test_nat_not_programmed_without_a_bridge_subnet(self, fs):
        """An unreadable bridge is logged and programmed nothing, rather than
        producing a rule with an empty source."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.get_bridge_subnet = mock.MagicMock(return_value="")
        daemon.run_commands = mock.MagicMock()
        daemon.log_error = mock.MagicMock()

        daemon.program_redfish_egress_nat(True)

        daemon.run_commands.assert_not_called()
        daemon.log_error.assert_called()

    @patchfs
    def test_feature_transitions_drive_the_nat(self, fs):
        """Enabling programs the masquerade, disabling removes it."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.disabled_daemon()
        daemon.program_redfish_egress_nat = mock.MagicMock()
        daemon.set_mgmt_interface_forwarding = mock.MagicMock()

        daemon.allow_redfish()
        self.assertTrue(daemon.RedfishAllowed)
        daemon.program_redfish_egress_nat.assert_called_with(True)
        daemon.set_mgmt_interface_forwarding.assert_called_with(True)

        daemon.block_redfish()
        self.assertFalse(daemon.RedfishAllowed)
        daemon.program_redfish_egress_nat.assert_called_with(False)
        daemon.set_mgmt_interface_forwarding.assert_called_with(False)

    @patchfs
    def test_mgmt_forwarding_sysctl(self, fs):
        """Enabling and disabling write the management interface forwarding
        flag, the half of the path that lets replies reach the bridge."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.run_commands = mock.MagicMock()

        daemon.set_mgmt_interface_forwarding(True)
        self.assertEqual([tuple(c) for c in daemon.run_commands.call_args[0][0]],
                         [('sysctl', '-w', 'net.ipv4.conf.eth0.forwarding=1')])

        daemon.run_commands.reset_mock()
        daemon.set_mgmt_interface_forwarding(False)
        self.assertEqual([tuple(c) for c in daemon.run_commands.call_args[0][0]],
                         [('sysctl', '-w', 'net.ipv4.conf.eth0.forwarding=0')])

    @patchfs
    def test_walk_reconciles_the_nat(self, fs):
        """nat is not rebuilt by the ruleset walk, so the walk reconciles it:
        a flushed nat table recovers without waiting for a feature transition."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.run_commands = mock.MagicMock()
        daemon.update_control_plane_nat_acls = mock.MagicMock()
        daemon.program_redfish_egress_nat = mock.MagicMock()
        daemon.set_mgmt_interface_forwarding = mock.MagicMock()

        daemon.update_control_plane_acls('', MockConfigDb())
        daemon.program_redfish_egress_nat.assert_called_once_with(True)
        daemon.set_mgmt_interface_forwarding.assert_called_once_with(True)

    @patchfs
    def test_walk_does_not_reconcile_when_disabled_or_in_a_namespace(self, fs):
        """Reconcile is host namespace only, and only while the feature is on."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.disabled_daemon()
        daemon.run_commands = mock.MagicMock()
        daemon.update_control_plane_nat_acls = mock.MagicMock()
        daemon.program_redfish_egress_nat = mock.MagicMock()
        daemon.set_mgmt_interface_forwarding = mock.MagicMock()
        daemon.update_control_plane_acls('', MockConfigDb())
        daemon.program_redfish_egress_nat.assert_not_called()
        daemon.set_mgmt_interface_forwarding.assert_not_called()

        daemon = self.enabled_daemon()
        daemon.iptables_cmd_ns_prefix['asic0'] = []
        daemon.run_commands = mock.MagicMock()
        daemon.update_control_plane_nat_acls = mock.MagicMock()
        daemon.program_redfish_egress_nat = mock.MagicMock()
        daemon.set_mgmt_interface_forwarding = mock.MagicMock()
        daemon.update_control_plane_acls('asic0', MockConfigDb())
        daemon.program_redfish_egress_nat.assert_not_called()
        daemon.set_mgmt_interface_forwarding.assert_not_called()

    # --- the bridge subnet read --------------------------------------------

    @patchfs
    def test_bridge_subnet_is_the_network_not_the_address(self, fs):
        """The gateway address docker0 carries is converted to its network, so
        the masquerade matches every container on the bridge."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.run_commands_pipe = mock.MagicMock(return_value='240.127.1.1/24')
        self.assertEqual(daemon.get_bridge_subnet(), BRIDGE_SUBNET)

    @patchfs
    def test_bridge_subnet_empty_when_unreadable_or_malformed(self, fs):
        """No docker0, or output that is not an interface address, yields ""
        rather than raising out of the feature handler."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.enabled_daemon()
        daemon.run_commands_pipe = mock.MagicMock(return_value='')
        self.assertEqual(daemon.get_bridge_subnet(), "")

        daemon.run_commands_pipe = mock.MagicMock(return_value='not-an-address')
        self.assertEqual(daemon.get_bridge_subnet(), "")
