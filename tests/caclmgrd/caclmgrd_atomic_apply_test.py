# Copyright 2026 Nexthop Systems Inc. All rights reserved.

import os
import sys

from swsscommon import swsscommon
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs
from sonic_py_common.general import load_module_from_source

from tests.common.mock_configdb import MockConfigDb

DBCONFIG_PATH = '/var/run/redis/sonic-db/database_config.json'

CONFIG_DB = {
    "DEVICE_METADATA": {
        "localhost": {
            "type": "ToRRouter",
        }
    },
    "FEATURE": {},
}

IP2ME_V4 = ['iptables', '-A', 'INPUT', '-d', '10.0.0.58/32', '-j', 'DROP']
IP2ME_V6 = ['ip6tables', '-A', 'INPUT', '-d', 'fc00::75/128', '-j', 'DROP']


class Proc(object):
    def __init__(self, returncode):
        self.returncode = returncode
        self.input = None

    def communicate(self, input=None):
        self.input = input
        return ('', 'simulated error\n' if self.returncode else '')


class TestCaclmgrdAtomicApply(TestCase):
    """
        The ACL rebuild must reach the kernel as one restore transaction per address
        family: applied command by command, flushing INPUT drops the only conntrack
        match and connection tracking stops until it is re-added.
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)

    def make_manager(self, fs):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)
        MockConfigDb.set_config_db(CONFIG_DB)
        cls = self.caclmgrd.ControlPlaneAclManager
        with mock.patch.object(cls, 'get_namespace_mgmt_ip'), mock.patch.object(cls, 'get_namespace_mgmt_ipv6'):
            mgr = cls("caclmgrd")
        mgr.log_info = mock.MagicMock()
        mgr.log_error = mock.MagicMock()
        mgr.get_chain_list = mock.MagicMock(return_value=['INPUT', 'FORWARD', 'OUTPUT'])
        mgr.get_chassis_midplane_interface_ip = mock.MagicMock(return_value='')
        mgr.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(return_value=[IP2ME_V4, IP2ME_V6])
        mgr.update_control_plane_nat_acls = mock.MagicMock()
        mgr.vxlan_rule_state = mock.MagicMock(return_value=1)
        return mgr

    def rebuild(self, mgr, rc_by_binary):
        procs = []

        def popen(cmd, **kwargs):
            p = Proc(rc_by_binary.get(cmd[0], 0))
            procs.append((list(cmd), p))
            return p
        with mock.patch("caclmgrd.subprocess.Popen", side_effect=popen):
            mgr.update_control_plane_acls('', MockConfigDb())
        return procs

    def test_restore_input_groups_by_table(self):
        f = self.caclmgrd.ControlPlaneAclManager.iptables_restore_input
        v4 = [
            ['iptables', '-P', 'INPUT', 'ACCEPT'],
            ['iptables', '-F', 'INPUT'],
            ['iptables', '-A', 'INPUT', '-s', '127.0.0.1', '-i', 'lo', '-j', 'ACCEPT'],
            ['iptables', '-A', 'INPUT', '-p', 'tcp', '--dport', '2514', '-j', 'ACCEPT', '-m', 'comment', '--comment', 'a b'],
            ['iptables', '-A', 'INPUT', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
        ]
        self.assertEqual(f(v4, [], 'iptables'),
                         '*filter\n'
                         '-P INPUT ACCEPT\n'
                         '-F INPUT\n'
                         '-A INPUT -s 127.0.0.1 -i lo -j ACCEPT\n'
                         '-A INPUT -p tcp --dport 2514 -j ACCEPT -m comment --comment "a b"\n'
                         '-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n'
                         'COMMIT\n')
        v6 = [
            ['ip6tables', '-F'],
            ['ip6tables', '-t', 'raw', '-F'],
            ['ip6tables', '-A', 'INPUT', '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
            ['ip6tables', '-t', 'raw', '-A', 'PREROUTING', '-p', 'ipv6-icmp', '-j', 'NOTRACK'],
        ]
        self.assertEqual(f(v6, [], 'ip6tables'),
                         '*filter\n'
                         '-F\n'
                         '-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n'
                         'COMMIT\n'
                         '*raw\n'
                         '-F\n'
                         '-A PREROUTING -p ipv6-icmp -j NOTRACK\n'
                         'COMMIT\n')

    def test_restore_input_strips_namespace_prefix(self):
        prefix = ['ip', 'netns', 'exec', 'asic0']
        got = self.caclmgrd.ControlPlaneAclManager.iptables_restore_input(
            [prefix + ['ip6tables', '-F', 'INPUT']], prefix, 'ip6tables')
        self.assertEqual(got, '*filter\n-F INPUT\nCOMMIT\n')

    def test_restore_input_refuses_untranslatable_commands(self):
        f = self.caclmgrd.ControlPlaneAclManager.iptables_restore_input
        for arg in ('say "hi"', '', 'a\\b', 'a\nb', 'x\0y'):
            self.assertIsNone(f([['iptables', '-A', 'INPUT', '-m', 'comment', '--comment', arg]], [], 'iptables'), repr(arg))
        self.assertIsNone(f([['ip6tables', '-F']], [], 'iptables'))
        self.assertIsNone(f([['iptables', '-t']], [], 'iptables'))
        self.assertIsNone(f([['iptables', '-t', 'raw']], [], 'iptables'))
        self.assertIsNone(f([['iptables', '-A', 'INPUT', '-t', 'nat', '-j', 'ACCEPT']], [], 'iptables'))
        self.assertIsNone(f([['iptables', '-F']], ['ip', 'netns', 'exec', 'asic0'], 'iptables'))

    @patchfs
    def test_rebuild_is_one_restore_per_family(self, fs):
        mgr = self.make_manager(fs)
        procs = self.rebuild(mgr, {})
        self.assertEqual([c for c, _ in procs], [['iptables-restore', '--noflush', '--wait=30'],
                                                ['ip6tables-restore', '--noflush', '--wait=30']])
        for cmd, proc in procs:
            sections = [line for line in proc.input.splitlines() if line.startswith('*')]
            self.assertEqual(sections.count('*filter'), 1)
            lines = proc.input.splitlines()
            flush = lines.index('-F INPUT' if cmd[0] == 'iptables-restore' else '-F')
            ct = lines.index('-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT')
            drop = lines.index(' '.join((IP2ME_V4 if cmd[0] == 'iptables-restore' else IP2ME_V6)[1:]))
            # flush, conntrack ACCEPT and ip2me DROP all sit in the one filter transaction
            self.assertTrue(lines.index('*filter') < flush < ct < drop < lines.index('COMMIT', drop))
        mgr.log_error.assert_not_called()

    @patchfs
    def test_failed_restore_falls_back_for_that_family_only(self, fs):
        mgr = self.make_manager(fs)
        procs = self.rebuild(mgr, {'iptables-restore': 1})
        cmds = [c for c, _ in procs]
        self.assertEqual(cmds[0], ['iptables-restore', '--noflush', '--wait=30'])
        self.assertIn(['iptables', '-F', 'INPUT'], cmds)
        self.assertIn(IP2ME_V4, cmds)
        self.assertIn(['ip6tables-restore', '--noflush', '--wait=30'], cmds)
        self.assertFalse(any(c[0] == 'ip6tables' for c in cmds))
        self.assertEqual(mgr.log_error.call_count, 1)
        self.assertIn('iptables-restore failed', mgr.log_error.call_args[0][0])

    @patchfs
    def test_untranslatable_family_is_applied_one_by_one(self, fs):
        mgr = self.make_manager(fs)
        mgr.generate_block_ip2me_traffic_iptables_commands = mock.MagicMock(
            return_value=[IP2ME_V4, ['ip6tables', '-A', 'INPUT', '-m', 'comment', '--comment', 'say "hi"', '-j', 'DROP']])
        cmds = [c for c, _ in self.rebuild(mgr, {})]
        self.assertEqual([c for c in cmds if c[0].endswith('-restore')], [['iptables-restore', '--noflush', '--wait=30']])
        self.assertFalse(any(c[0] == 'iptables' for c in cmds))
        self.assertIn(['ip6tables', '-F'], cmds)
        mgr.log_error.assert_called_once()
