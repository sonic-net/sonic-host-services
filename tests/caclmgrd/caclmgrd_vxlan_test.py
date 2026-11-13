import ipaddress
import os
import sys
import threading
from swsscommon import swsscommon

from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock
from pyfakefs.fake_filesystem_unittest import patchfs

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

LO_V4 = ('-s', '127.0.0.1', '-i', 'lo', '-j', 'ACCEPT')
LO_V6 = ('-s', '::1', '-i', 'lo', '-j', 'ACCEPT')
BFD_V4 = ('-p', 'udp', '-m', 'multiport', '--dports', '3784,4784,6784', '-j', 'ACCEPT', '!', '-i', 'eth0')


def vxlan_spec(src_ip):
    return ('-p', 'udp', '-d', src_ip, '--dport', '4789', '-j', 'ACCEPT', '!', '-i', 'eth0')


class FakeProc(object):
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self._out = (stdout, stderr)

    def communicate(self):
        return self._out


def canonical(spec):
    """iptables matches addresses by value, not spelling"""
    spec = list(spec)
    if '-d' in spec:
        i = spec.index('-d') + 1
        spec[i] = str(ipaddress.ip_address(spec[i]))
    return tuple(spec)


class FakeIptables(object):
    """
        Models the filter INPUT chain of iptables and ip6tables closely enough to
        reproduce rule-position failures: '-I INPUT <n>' fails once n exceeds the
        chain length + 1, as the real binaries do.
    """
    def __init__(self):
        self.chains = {'iptables': [], 'ip6tables': []}
        self.calls = []
        self.fail_insert = 0
        self.check_rc = None
        self.on_flush = None

    def popen(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        ipt = cmd[0]
        if ipt not in self.chains or len(cmd) < 2:
            return FakeProc(0)
        chain = self.chains[ipt]
        op = cmd[1]
        if op == '-F' and (len(cmd) == 2 or cmd[2] == 'INPUT'):
            del chain[:]
            if self.on_flush and ipt == 'iptables':
                self.on_flush()
        elif op == '-S' and cmd[2] == 'INPUT':
            return FakeProc(0, self.render(ipt))
        elif op == '-A' and cmd[2] == 'INPUT':
            chain.append(tuple(cmd[3:]))
        elif op == '-I' and cmd[2] == 'INPUT':
            pos = int(cmd[3])
            if self.fail_insert:
                self.fail_insert -= 1
                return FakeProc(1, '', 'iptables: simulated failure.\n')
            if pos > len(chain) + 1:
                return FakeProc(1, '', 'iptables: Index of insertion too big.\n')
            chain.insert(pos - 1, tuple(cmd[4:]))
        elif op == '-C' and cmd[2] == 'INPUT':
            if self.check_rc is not None:
                return FakeProc(self.check_rc)
            return FakeProc(0 if self.find(chain, cmd[3:]) is not None else 1)
        elif op == '-D' and cmd[2] == 'INPUT':
            i = self.find(chain, cmd[3:])
            if i is None:
                return FakeProc(1, '', 'iptables: Bad rule (does a matching rule exist in that chain?).\n')
            del chain[i]
        return FakeProc(0)

    @staticmethod
    def find(chain, spec):
        for i, rule in enumerate(chain):
            if canonical(rule) == canonical(spec):
                return i
        return None

    def render(self, ipt):
        lines = ['-P INPUT ACCEPT']
        for spec in self.chains[ipt]:
            spec = list(spec)
            if '-s' in spec and '/' not in spec[spec.index('-s') + 1]:
                i = spec.index('-s') + 1
                spec[i] += '/128' if ':' in spec[i] else '/32'
            lines.append('-A INPUT ' + ' '.join(spec))
        return '\n'.join(lines) + '\n'

    def count(self, src_ip):
        ipt = 'ip6tables' if ':' in src_ip else 'iptables'
        return [canonical(r) for r in self.chains[ipt]].count(canonical(vxlan_spec(src_ip)))


class TestCaclmgrdVxlan(TestCase):
    """
        Test caclmgrd VxLAN 4789 ACCEPT rule handling
    """
    def setUp(self):
        swsscommon.ConfigDBConnector = MockConfigDb
        test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        modules_path = os.path.dirname(test_path)
        scripts_path = os.path.join(modules_path, "scripts")
        sys.path.insert(0, modules_path)
        caclmgrd_path = os.path.join(scripts_path, 'caclmgrd')
        self.caclmgrd = load_module_from_source('caclmgrd', caclmgrd_path)
        self.ipt = FakeIptables()
        # A built INPUT chain: loopback ACCEPT first, then the BFD rule
        self.ipt.chains['iptables'] = [LO_V4, BFD_V4]
        self.ipt.chains['ip6tables'] = [LO_V6]

    def run_patched(self, fs, body):
        if not os.path.exists(DBCONFIG_PATH):
            fs.create_file(DBCONFIG_PATH)
        MockConfigDb.set_config_db(CONFIG_DB)
        with mock.patch("caclmgrd.ControlPlaneAclManager.run_commands_pipe", return_value=''), \
                mock.patch("caclmgrd.subprocess.Popen", side_effect=self.ipt.popen):
            mgr = self.caclmgrd.ControlPlaneAclManager("caclmgrd")
            mgr.log_info = mock.MagicMock()
            mgr.log_error = mock.MagicMock()
            body(mgr)

    @patchfs
    def test_set_inserts_after_loopback_accept(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.assertEqual(self.ipt.chains['iptables'], [LO_V4, vxlan_spec('10.1.0.32'), BFD_V4])
            self.assertEqual(mgr.vxlan_installed[''], {'10.1.0.32'})
            mgr.log_info.assert_any_call("Enabled vxlan port for source ip 10.1.0.32")
            # a repeated SET does not duplicate the rule
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            mgr.update_vxlan_tunnel('vtep1', 'DEL', [])
            self.assertEqual(self.ipt.chains['iptables'], [LO_V4, BFD_V4])
            self.assertFalse(mgr.vxlan_rules_pending())
            mgr.log_error.assert_not_called()
        self.run_patched(fs, body)

    @patchfs
    def test_ipv6_vtep(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', 'fc00:1::32')])
            self.assertEqual(self.ipt.chains['ip6tables'], [LO_V6, vxlan_spec('fc00:1::32')])
            self.assertEqual(self.ipt.chains['iptables'], [LO_V4, BFD_V4])
            mgr.update_vxlan_tunnel('vtep1', 'DEL', [])
            self.assertEqual(self.ipt.chains['ip6tables'], [LO_V6])
        self.run_patched(fs, body)

    @patchfs
    def test_set_without_loopback_rule_inserts_at_top(self, fs):
        def body(mgr):
            self.ipt.chains['iptables'] = []
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.assertEqual(self.ipt.chains['iptables'], [vxlan_spec('10.1.0.32')])
            mgr.log_error.assert_not_called()
        self.run_patched(fs, body)

    @patchfs
    def test_failed_insert_is_not_marked_enabled_and_retried(self, fs):
        def body(mgr):
            self.ipt.fail_insert = 2
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.assertEqual(self.ipt.count('10.1.0.32'), 0)
            self.assertEqual(mgr.vxlan_installed[''], set())
            self.assertTrue(mgr.vxlan_rules_pending())
            for call in mgr.log_info.call_args_list:
                self.assertNotIn("Enabled vxlan port", call[0][0])
            mgr.sync_all_vxlan_rules()
            self.assertEqual(mgr.log_error.call_count, 1)  # a failure is logged once, not per retry
            mgr.sync_all_vxlan_rules()
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            self.assertFalse(mgr.vxlan_rules_pending())
        self.run_patched(fs, body)

    @patchfs
    def test_src_ip_change_replaces_rule(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.33')])
            self.assertEqual(self.ipt.count('10.1.0.32'), 0)
            self.assertEqual(self.ipt.count('10.1.0.33'), 1)
            self.assertEqual(mgr.vxlan_installed[''], {'10.1.0.33'})
            # SET without src_ip drops the tunnel's rule
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('dst_ip', '10.1.0.99')])
            self.assertEqual(self.ipt.count('10.1.0.33'), 0)
        self.run_patched(fs, body)

    @patchfs
    def test_invalid_src_ip_is_ignored(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', 'not-an-ip')])
            self.assertEqual(mgr.vxlan_src_ips, {})
            self.assertEqual(self.ipt.chains['iptables'], [LO_V4, BFD_V4])
        self.run_patched(fs, body)

    @patchfs
    def test_multiple_tunnels(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep_v4', 'SET', [('src_ip', '10.1.0.32')])
            mgr.update_vxlan_tunnel('vtep_v6', 'SET', [('src_ip', 'fc00:1::32')])
            mgr.update_vxlan_tunnel('vnet_vtep', 'SET', [('src_ip', '10.1.0.32')])
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            self.assertEqual(self.ipt.count('fc00:1::32'), 1)
            # the v4 address is still used by another tunnel
            mgr.update_vxlan_tunnel('vtep_v4', 'DEL', [])
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            mgr.update_vxlan_tunnel('vnet_vtep', 'DEL', [])
            self.assertEqual(self.ipt.count('10.1.0.32'), 0)
            self.assertEqual(self.ipt.count('fc00:1::32'), 1)
            mgr.update_vxlan_tunnel('vtep_v6', 'DEL', [])
            self.assertEqual(self.ipt.count('fc00:1::32'), 0)
        self.run_patched(fs, body)

    @patchfs
    def test_del_removes_duplicates(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.ipt.chains['iptables'].append(vxlan_spec('10.1.0.32'))
            mgr.update_vxlan_tunnel('vtep1', 'DEL', [])
            self.assertEqual(self.ipt.count('10.1.0.32'), 0)
        self.run_patched(fs, body)

    @patchfs
    def test_rebuild_restores_rule(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            mgr.num_changes[''] = 1
            mgr.thread_exceptions = {}
            mgr.get_chain_list = mock.MagicMock(return_value=['INPUT', 'FORWARD', 'OUTPUT'])
            with mock.patch.object(self.caclmgrd.ControlPlaneAclManager, 'UPDATE_DELAY_SECS', 0):
                mgr.check_and_update_control_plane_acls('', 1)
            chain = self.ipt.chains['iptables']
            self.assertEqual(chain[0][:4], ('-s', '127.0.0.1', '-i', 'lo'))
            self.assertEqual(chain[1], vxlan_spec('10.1.0.32'))
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            self.assertEqual(mgr.vxlan_installed[''], {'10.1.0.32'})
            self.assertIn(['iptables', '-I', 'INPUT', '2'] + list(vxlan_spec('10.1.0.32')), self.ipt.calls)
        self.run_patched(fs, body)

    @patchfs
    def test_set_waits_for_rebuild_in_progress(self, fs):
        """
            The ACL rebuild flushes INPUT and refills it. A VXLAN_TUNNEL SET arriving
            meanwhile must wait for it instead of inserting into the half-built chain,
            where '-I INPUT 2' fails or the rule is flushed away.
        """
        def body(mgr):
            flushed, release, set_done = threading.Event(), threading.Event(), threading.Event()

            def hold_after_flush():
                flushed.set()
                release.wait(5)
            self.ipt.on_flush = hold_after_flush

            mgr.num_changes[''] = 1
            mgr.thread_exceptions = {}
            mgr.get_chain_list = mock.MagicMock(return_value=['INPUT', 'FORWARD', 'OUTPUT'])

            def set_tunnel():
                mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
                set_done.set()

            with mock.patch.object(self.caclmgrd.ControlPlaneAclManager, 'UPDATE_DELAY_SECS', 0):
                rebuild = threading.Thread(target=mgr.check_and_update_control_plane_acls, args=('', 1))
                setter = threading.Thread(target=set_tunnel)
                try:
                    rebuild.start()
                    self.assertTrue(flushed.wait(5))
                    setter.start()
                    self.assertFalse(set_done.wait(0.3))
                    self.assertFalse(any('4789' in c for c in self.ipt.calls))
                finally:
                    # never leave a thread running past the mocks
                    release.set()
                    rebuild.join(5)
                    if setter.is_alive() or set_done.is_set():
                        setter.join(5)
            self.assertTrue(set_done.is_set())
            chain = self.ipt.chains['iptables']
            self.assertEqual(chain[0][:4], ('-s', '127.0.0.1', '-i', 'lo'))
            self.assertEqual(chain[1], vxlan_spec('10.1.0.32'))
            self.assertEqual(self.ipt.count('10.1.0.32'), 1)
            self.assertEqual(mgr.vxlan_installed[''], {'10.1.0.32'})
            mgr.log_error.assert_not_called()
        self.run_patched(fs, body)

    @patchfs
    def test_ipv6_spellings_share_one_rule(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('a', 'SET', [('src_ip', 'FC00:1::32')])
            mgr.update_vxlan_tunnel('b', 'SET', [('src_ip', 'fc00:1:0::32')])
            self.assertEqual(self.ipt.count('fc00:1::32'), 1)
            mgr.update_vxlan_tunnel('a', 'DEL', [])
            self.assertEqual(self.ipt.count('fc00:1::32'), 1)
            self.assertFalse(mgr.vxlan_rules_pending())
        self.run_patched(fs, body)

    @patchfs
    def test_check_error_does_not_count_as_removed(self, fs):
        def body(mgr):
            mgr.update_vxlan_tunnel('vtep1', 'SET', [('src_ip', '10.1.0.32')])
            self.ipt.check_rc = 2
            mgr.update_vxlan_tunnel('vtep1', 'DEL', [])
            self.assertEqual(mgr.vxlan_installed[''], {'10.1.0.32'})
            self.assertTrue(mgr.vxlan_rules_pending())
            mgr.sync_all_vxlan_rules()
            self.assertEqual(mgr.log_error.call_count, 1)
            self.ipt.check_rc = None
            mgr.sync_all_vxlan_rules()
            self.assertEqual(self.ipt.count('10.1.0.32'), 0)
            self.assertFalse(mgr.vxlan_rules_pending())
        self.run_patched(fs, body)
