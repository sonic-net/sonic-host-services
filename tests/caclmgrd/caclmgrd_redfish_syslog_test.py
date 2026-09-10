import os
import sys

from swsscommon import swsscommon
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

from tests.common.mock_configdb import MockConfigDb


DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

# Must stay byte-identical to the container-side -C check in docker_image_ctl.j2.
REDFISH_SYSLOG_RULE = (
    'iptables', '-A', 'INPUT', '-i', 'docker0', '-p', 'tcp', '--dport', '2514',
    '-j', 'ACCEPT', '-m', 'comment', '--comment', 'redfish_syslog',
)


class TestCaclmgrdRedfishSyslog(TestCase):
    """
        Verifies caclmgrd owns the redfish docker0 syslog (RELP tcp/2514) INPUT
        exception, gated on FEATURE.redfish, and re-emits it before the
        catch-all DROP on every rebuild.

        redfish is bridge-networked, so its rsyslog forwards over RELP to the
        docker0 gateway instead of 127.0.0.1. That traffic arrives on docker0
        (not lo), so it is not covered by the loopback ACCEPT and would be
        swept into the control-plane catch-all DROP without this exception.
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
        """RedfishAllowed is seeded from the persisted FEATURE state at __init__."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        enabled = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                     "FEATURE": {"redfish": {"state": "enabled"}}})
        disabled = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                      "FEATURE": {"redfish": {"state": "disabled"}}})
        absent = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {}})
        self.assertTrue(enabled.RedfishAllowed)
        self.assertFalse(disabled.RedfishAllowed)
        self.assertFalse(absent.RedfishAllowed)

    @patchfs
    def test_rule_emitted_when_feature_enabled(self, fs):
        """When enabled, the docker0/2514 ACCEPT rule is present in the host rebuild."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"redfish": {"state": "enabled"}}})
        self.assertTrue(daemon.RedfishAllowed)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertIn(REDFISH_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_rule_absent_when_feature_disabled(self, fs):
        """When disabled, no exception is programmed."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"redfish": {"state": "disabled"}}})
        self.assertFalse(daemon.RedfishAllowed)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertNotIn(REDFISH_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_rule_absent_when_feature_entry_missing(self, fs):
        """Images built without redfish have no FEATURE entry at all -- no exception."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {}})
        self.assertFalse(daemon.RedfishAllowed)

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        self.assertNotIn(REDFISH_SYSLOG_RULE, [tuple(c) for c in cmds])

    @patchfs
    def test_rule_reinserted_before_catch_all_drop(self, fs):
        """With a CACL rule present (so the catch-all DROP exists), the exception appears
        AND strictly before the DROP -- there is never a rebuild window where a DROP
        exists without it."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({
            "ACL_TABLE": {
                "SSH_ONLY": {"stage": "INGRESS", "type": "CTRLPLANE", "services": ["SSH"]},
            },
            "ACL_RULE": {
                "SSH_ONLY|RULE_1": {"PACKET_ACTION": "ACCEPT", "PRIORITY": "9999", "SRC_IP": "10.0.0.0/8"},
            },
            "DEVICE_METADATA": {"localhost": {}},
            "FEATURE": {"redfish": {"state": "enabled"}},
        })

        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('', MockConfigDb())
        cmds = [tuple(c) for c in cmds]
        catch_all_drop = ('iptables', '-A', 'INPUT', '-j', 'DROP')
        self.assertIn(REDFISH_SYSLOG_RULE, cmds, "exception must be present in rebuild")
        self.assertIn(catch_all_drop, cmds, "test setup should produce a catch-all DROP")
        self.assertLess(cmds.index(REDFISH_SYSLOG_RULE), cmds.index(catch_all_drop),
                        "exception must come before the catch-all DROP")

    @patchfs
    def test_rule_host_namespace_only(self, fs):
        """docker0 lives in the host namespace, so asic namespaces get no exception."""
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)

        daemon = self.setup_daemon({"DEVICE_METADATA": {"localhost": {}},
                                    "FEATURE": {"redfish": {"state": "enabled"}}})
        self.assertTrue(daemon.RedfishAllowed)

        daemon.iptables_cmd_ns_prefix['asic0'] = []
        cmds, _ = daemon.get_acl_rules_and_translate_to_iptables_commands('asic0', MockConfigDb())
        self.assertNotIn(REDFISH_SYSLOG_RULE, [tuple(c) for c in cmds])
