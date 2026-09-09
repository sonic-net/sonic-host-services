import os
import sys

from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

# Must stay byte-identical to the container-side -C check in docker_image_ctl.j2.
DHCP_SYSLOG_RULE = (
    'iptables', '-A', 'INPUT', '-i', 'docker0', '-p', 'tcp', '--dport', '2514',
    '-j', 'ACCEPT', '-m', 'comment', '--comment', 'dhcp_server_syslog',
)


class TestCaclmgrdDhcpServerSyslog(TestCase):
    """
        Verifies caclmgrd owns the dhcp_server docker0 syslog (RELP tcp/2514) INPUT
        exception, gated on FEATURE.dhcp_server, and re-emits it before the
        catch-all DROP on every rebuild (sonic-net/sonic-buildimage#27584).
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
    def test_init_seeds_flag_from_feature_state(self, fs):
        """DhcpServerSyslogAllowed is seeded from the persisted FEATURE state at __init__."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        enabled = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                     "FEATURE": {"dhcp_server": {"state": "enabled"}}})
        self.assertTrue(enabled.DhcpServerSyslogAllowed)

        disabled = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                      "FEATURE": {"dhcp_server": {"state": "disabled"}}})
        self.assertFalse(disabled.DhcpServerSyslogAllowed)

        # Feature absent entirely (platform without dhcp_server) -> not allowed.
        absent = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}}, "FEATURE": {}})
        self.assertFalse(absent.DhcpServerSyslogAllowed)

    @patchfs
    def test_rule_emitted_when_feature_enabled(self, fs):
        """When enabled, the docker0/2514 ACCEPT rule is present in the host rebuild."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"dhcp_server": {"state": "enabled"}}})
        self.assertTrue(daemon.DhcpServerSyslogAllowed)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertIn(DHCP_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_rule_absent_when_feature_disabled(self, fs):
        """When disabled, the rule is not emitted, so the next rebuild drops it."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"dhcp_server": {"state": "disabled"}}})
        self.assertFalse(daemon.DhcpServerSyslogAllowed)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertNotIn(DHCP_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_rule_reinserted_before_catch_all_drop(self, fs):
        """With a CACL rule present (so the catch-all DROP exists), the exception appears
        AND strictly before the DROP -- there is never a rebuild window where a DROP
        exists without it."""
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
            "FEATURE": {"dhcp_server": {"state": "enabled"}},
        }
        daemon = self.setup_daemon(config_db)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        catch_all_drop = ('iptables', '-A', 'INPUT', '-j', 'DROP')
        self.assertIn(DHCP_SYSLOG_RULE, cmds, "exception must be present in rebuild")
        self.assertIn(catch_all_drop, cmds, "test setup should produce a catch-all DROP")
        self.assertLess(cmds.index(DHCP_SYSLOG_RULE), cmds.index(catch_all_drop),
                        "exception must come before the catch-all DROP")

    @patchfs
    def test_rule_host_namespace_only(self, fs):
        """docker0 is a host bridge / IPv4 only; ASIC namespaces must not emit the rule
        even when the flag is set."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"dhcp_server": {"state": "enabled"}}})
        self.assertTrue(daemon.DhcpServerSyslogAllowed)
        daemon.iptables_cmd_ns_prefix['asic0'] = []

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('asic0', MockConfigDb())
        self.assertNotIn(DHCP_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_handle_feature_state_events_dhcp_server(self, fs):
        """Drive the FEATURE-table handler for the dhcp_server branch: enable transition,
        disable transition, unrelated key ignored, and same-state no-op."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"dhcp_server": {"state": "disabled"}}})
        self.assertFalse(daemon.DhcpServerSyslogAllowed)

        def sub_with(events):
            sub = mock.MagicMock()
            sub.pop.side_effect = events + [("", None, None)]
            return sub

        # enable: flag flips True and namespace queued for re-walk
        notif = set()
        daemon.handle_feature_state_events(
            sub_with([("dhcp_server", "SET", (("state", "enabled"),))]), "", notif)
        self.assertTrue(daemon.DhcpServerSyslogAllowed)
        self.assertIn("", notif)

        # disable: flag flips False and namespace queued
        notif = set()
        daemon.handle_feature_state_events(
            sub_with([("dhcp_server", "SET", (("state", "disabled"),))]), "", notif)
        self.assertFalse(daemon.DhcpServerSyslogAllowed)
        self.assertIn("", notif)

        # unrelated FEATURE event: dhcp_server flag untouched, nothing queued
        notif = set()
        daemon.handle_feature_state_events(
            sub_with([("bgp", "SET", (("state", "enabled"),))]), "", notif)
        self.assertFalse(daemon.DhcpServerSyslogAllowed)
        self.assertEqual(notif, set())

        # same-state event (already disabled): no-op, nothing queued
        notif = set()
        daemon.handle_feature_state_events(
            sub_with([("dhcp_server", "SET", (("state", "disabled"),))]), "", notif)
        self.assertFalse(daemon.DhcpServerSyslogAllowed)
        self.assertEqual(notif, set())
