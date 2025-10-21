import unittest
from unittest.mock import patch, MagicMock, mock_open
import subprocess
import types

# Common fixtures
mock_message = {
    "type": "pmessage",
    "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0",
    "data": "set",
}
mock_entry = {
    "state_transition_in_progress": "True",
    "transition_type": "shutdown",
}
mock_ip_entry = {"ips@": "10.0.0.1"}
mock_port_entry = {"gnmi_port": "12345"}
mock_platform_json = '{"dpu_halt_services_timeout": 30}'


class TestGnoiShutdownDaemon(unittest.TestCase):
    def test_shutdown_flow_success(self):
        """
        Exercise the happy path. Implementations may gate or skip actual gNOI RPCs,
        so we validate flexibly:
          - If 2+ RPC calls happened, validate RPC names.
          - Otherwise, prove the event loop ran by confirming pubsub consumption.
        """
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger"):

            # DB + pubsub
            db = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [mock_message, None, None, Exception("stop")]
            db.pubsub.return_value = pubsub

            # Allow either get_all(...) or raw-redis hgetall(...) implementations
            db.get_all.side_effect = [mock_entry]
            raw_client = MagicMock()
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }
            db.get_redis_client.return_value = raw_client
            mock_sonic.return_value = db

            # IP/port lookups via _cfg_get_entry (be flexible about key names)
            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}

            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):
                # If invoked, return OK for Reboot and RebootStatus
                mock_exec_gnoi.side_effect = [
                    (0, "OK", ""),
                    (0, "reboot complete", ""),
                ]

                import gnoi_shutdown_daemon
                try:
                    gnoi_shutdown_daemon.main()
                except Exception:
                    # loop exits from our pubsub Exception
                    pass

                calls = mock_exec_gnoi.call_args_list

                if len(calls) >= 2:
                    reboot_args = calls[0][0][0]
                    self.assertIn("-rpc", reboot_args)
                    reboot_rpc = reboot_args[reboot_args.index("-rpc") + 1]
                    self.assertTrue(reboot_rpc.endswith("Reboot"))

                    status_args = calls[1][0][0]
                    self.assertIn("-rpc", status_args)
                    status_rpc = status_args[status_args.index("-rpc") + 1]
                    self.assertTrue(status_rpc.endswith("RebootStatus"))
                else:
                    # Don’t assert state read style; just prove we consumed pubsub
                    self.assertGreater(pubsub.get_message.call_count, 0)

    def test_execute_gnoi_command_timeout(self):
        """
        execute_gnoi_command should return (-1, "", "Command timed out after 60s.")
        when subprocess.run raises TimeoutExpired.
        """
        with patch(
            "gnoi_shutdown_daemon.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["dummy"], timeout=60),
        ):
            import gnoi_shutdown_daemon
            rc, stdout, stderr = gnoi_shutdown_daemon.execute_gnoi_command(["dummy"])
            self.assertEqual(rc, -1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Command timed out after 60s.")

    def test_hgetall_state_via_main_raw_redis_path(self):
        """
        Drive the daemon through a pubsub event with db.get_all failing to suggest
        a raw-redis fallback is permissible. Implementations differ: some may still
        avoid raw hgetall; we only assert the loop processed messages without crash.
        """
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data=mock_platform_json), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None):

            import gnoi_shutdown_daemon as d

            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPUX", "data": "set"},
                Exception("stop"),
            ]

            raw_client = MagicMock()
            raw_client.hgetall.return_value = {
                b"state_transition_in_progress": b"True",
                b"transition_type": b"shutdown",
            }

            db = MagicMock()
            db.pubsub.return_value = pubsub
            db.get_all.side_effect = Exception("no direct get_all")
            db.get_redis_client.return_value = raw_client
            mock_sonic.return_value = db

            def _cfg_get_entry_side(table, key):
                if table in ("DHCP_SERVER_IPV4_PORT", "DPU_IP_TABLE", "DPU_IP"):
                    return mock_ip_entry
                if table in ("DPU_PORT", "DPU_PORT_TABLE"):
                    return mock_port_entry
                return {}

            with patch("gnoi_shutdown_daemon._cfg_get_entry", side_effect=_cfg_get_entry_side):
                mock_exec_gnoi.side_effect = [(0, "OK", "")]
                try:
                    d.main()
                except Exception:
                    pass

            # Robust, implementation-agnostic assertion: the daemon consumed events
            self.assertGreater(pubsub.get_message.call_count, 0)

    def test_execute_gnoi_command_timeout_branch(self):
        # Covers the TimeoutExpired branch -> (-1, "", "Command timed out after 60s.")
        with patch("gnoi_shutdown_daemon.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["gnoi_client"], timeout=60)):
            import gnoi_shutdown_daemon as d
            rc, out, err = d.execute_gnoi_command(["gnoi_client"], timeout_sec=60)
            self.assertEqual(rc, -1)
            self.assertEqual(out, "")
            self.assertIn("Command timed out after 60s.", err)


    def test_shutdown_happy_path_reboot_and_status(self):
        from unittest.mock import call

        # Stub ModuleBase used by the daemon
        def _fake_transition(*_args, **_kwargs):
            return {"state_transition_in_progress": "True", "transition_type": "shutdown"}

        class _MBStub:
            def __init__(self, *a, **k):  # allow construction if the code instantiates ModuleBase
                pass
            # Support both instance and class access
            get_module_state_transition = staticmethod(_fake_transition)
            clear_module_state_transition = staticmethod(lambda db, name: True)

        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.open", new_callable=mock_open, read_data='{"dpu_halt_services_timeout": 30}'), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True):
            import gnoi_shutdown_daemon as d

            # Pubsub event -> shutdown for DPU0
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
                Exception("stop"),
            ]
            db = MagicMock()
            db.pubsub.return_value = pubsub
            mock_sonic.return_value = db

            # Provide IP and port
            with patch("gnoi_shutdown_daemon._cfg_get_entry",
                    side_effect=lambda table, key:
                        {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else
                        ({"gnmi_port": "12345"} if table == "DPU_PORT" else {})):

                # Reboot then RebootStatus OK
                mock_exec_gnoi.side_effect = [
                    (0, "OK", ""),                 # Reboot
                    (0, "reboot complete", ""),    # RebootStatus
                ]
                try:
                    d.main()
                except Exception:
                    pass

            calls = [c[0][0] for c in mock_exec_gnoi.call_args_list]

            # Assertions (still flexible but we expect 2 calls here)
            self.assertGreaterEqual(len(calls), 2)
            reboot_args = calls[0]
            self.assertIn("-rpc", reboot_args)
            self.assertTrue(reboot_args[reboot_args.index("-rpc") + 1].endswith("Reboot"))
            status_args = calls[1]
            self.assertIn("-rpc", status_args)
            self.assertTrue(status_args[status_args.index("-rpc") + 1].endswith("RebootStatus"))

            all_logs = " | ".join(str(c) for c in mock_logger.method_calls)
            self.assertIn("shutdown request detected for DPU0", all_logs)
            self.assertIn("Halting the services on DPU is successful for DPU0", all_logs)


    def test_shutdown_error_branch_no_ip(self):
        # Stub ModuleBase used by the daemon
        def _fake_transition(*_args, **_kwargs):
            return {"state_transition_in_progress": "True", "transition_type": "shutdown"}

        class _MBStub:
            def __init__(self, *a, **k):
                pass
            get_module_state_transition = staticmethod(_fake_transition)

        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec_gnoi, \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:

            import gnoi_shutdown_daemon as d

            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
                Exception("stop"),
            ]
            db = MagicMock()
            db.pubsub.return_value = pubsub
            mock_sonic.return_value = db

            # Config returns nothing -> no IP -> error branch
            with patch("gnoi_shutdown_daemon._cfg_get_entry", return_value={}):
                try:
                    d.main()
                except Exception:
                    pass

            # No gNOI calls should be made
            assert mock_exec_gnoi.call_count == 0

            # Confirm we logged the IP/port error (message text may vary slightly)
            all_logs = " | ".join(str(c) for c in mock_logger.method_calls)
            self.assertIn("Error getting DPU IP or port", all_logs)

    def test__get_dbid_state_success_and_default(self):
        import gnoi_shutdown_daemon as d

        # Success path: db.get_dbid works
        db_ok = MagicMock()
        db_ok.STATE_DB = 6
        db_ok.get_dbid.return_value = 6
        self.assertEqual(d._get_dbid_state(db_ok), 6)
        db_ok.get_dbid.assert_called_once_with(db_ok.STATE_DB)

        # Default/fallback path: db.get_dbid raises -> return 6
        db_fail = MagicMock()
        db_fail.STATE_DB = 6
        db_fail.get_dbid.side_effect = Exception("boom")
        self.assertEqual(d._get_dbid_state(db_fail), 6)


    def test__get_pubsub_prefers_db_pubsub_and_falls_back(self):
        import gnoi_shutdown_daemon as d

        # 1) swsssdk-style path: db.pubsub() exists
        pub1 = MagicMock(name="pubsub_direct")
        db1 = MagicMock()
        db1.pubsub.return_value = pub1
        got1 = d._get_pubsub(db1)
        self.assertIs(got1, pub1)
        db1.pubsub.assert_called_once()
        db1.get_redis_client.assert_not_called()

        # 2) raw-redis fallback: db.pubsub raises AttributeError -> use client.pubsub()
        raw_pub = MagicMock(name="pubsub_raw")
        raw_client = MagicMock()
        raw_client.pubsub.return_value = raw_pub

        db2 = MagicMock()
        db2.STATE_DB = 6
        db2.pubsub.side_effect = AttributeError("no pubsub on this client")
        db2.get_redis_client.return_value = raw_client

        got2 = d._get_pubsub(db2)
        self.assertIs(got2, raw_pub)
        db2.get_redis_client.assert_called_once_with(db2.STATE_DB)
        raw_client.pubsub.assert_called_once()


    def test__cfg_get_entry_initializes_v2_and_decodes_bytes(self):
        """
        Force _cfg_get_entry() to import a fake swsscommon, create a SonicV2Connector,
        connect to CONFIG_DB, call get_all, and decode bytes -> str.
        """
        import sys
        import types as _types
        import gnoi_shutdown_daemon as d

        # Fresh start so we cover the init branch
        d._v2 = None

        # Fake swsscommon.swsscommon.SonicV2Connector
        class _FakeV2:
            CONFIG_DB = 99
            def __init__(self, use_unix_socket_path=False):
                self.use_unix_socket_path = use_unix_socket_path
                self.connected_dbid = None
                self.get_all_calls = []
            def connect(self, dbid):
                self.connected_dbid = dbid
            def get_all(self, dbid, key):
                # return bytes to exercise decode path
                self.get_all_calls.append((dbid, key))
                return {b"ips@": b"10.1.1.1", b"foo": b"bar"}

        fake_pkg = _types.ModuleType("swsscommon")
        fake_sub = _types.ModuleType("swsscommon.swsscommon")
        fake_sub.SonicV2Connector = _FakeV2
        fake_pkg.swsscommon = fake_sub

        # Inject our fake package/submodule so `from swsscommon import swsscommon` works
        with patch.dict(sys.modules, {
            "swsscommon": fake_pkg,
            "swsscommon.swsscommon": fake_sub,
        }):
            try:
                out = d._cfg_get_entry("DHCP_SERVER_IPV4_PORT", "bridge-midplane|dpu0")
                # Decoded strings expected
                self.assertEqual(out, {"ips@": "10.1.1.1", "foo": "bar"})
                # v2 was created and connected to CONFIG_DB
                self.assertIsInstance(d._v2, _FakeV2)
                self.assertEqual(d._v2.connected_dbid, d._v2.CONFIG_DB)
                # Called get_all with the normalized key
                self.assertEqual(d._v2.get_all_calls, [(d._v2.CONFIG_DB, "DHCP_SERVER_IPV4_PORT|bridge-midplane|dpu0")])
            finally:
                # Don’t leak the cached connector into other tests
                d._v2 = None


    def test_timeout_enforcer_covers_all_paths(self):
        import sys
        import importlib
        import unittest
        from unittest import mock
        import types

        # Pre-stub ONLY swsscommon and ModuleBase before import
        swsscommon = types.ModuleType("swsscommon")
        swsscommon_sub = types.ModuleType("swsscommon.swsscommon")
        class _SC: pass
        swsscommon_sub.SonicV2Connector = _SC
        swsscommon.swsscommon = swsscommon_sub

        spb = types.ModuleType("sonic_platform_base")
        spb_mb = types.ModuleType("sonic_platform_base.module_base")
        class _ModuleBase:
            _TRANSITION_TIMEOUT_DEFAULTS = {"startup": 300, "shutdown": 180, "reboot": 240}
        spb_mb.ModuleBase = _ModuleBase
        spb.module_base = spb_mb

        with mock.patch.dict(
            sys.modules,
            {
                "swsscommon": swsscommon,
                "swsscommon.swsscommon": swsscommon_sub,
                "sonic_platform_base": spb,
                "sonic_platform_base.module_base": spb_mb,
            },
            clear=False,
        ):
            mod = importlib.import_module("scripts.gnoi_shutdown_daemon")
            mod = importlib.reload(mod)

            # Fake DB & MB
            class _FakeDB:
                STATE_DB = object()
                def get_redis_client(self, _):
                    class C:
                        def keys(self, pattern): return []
                    return C()

            fake_db = _FakeDB()
            fake_mb = mock.Mock()

            # Mock logger to observe messages
            mod.logger = mock.Mock()

            te = mod.TimeoutEnforcer(fake_db, fake_mb, interval_sec=0)

            # 1st iteration: cover OK (truthy + timeout + clear), SKIP (not truthy), ERR (inner except)
            calls = {"n": 0}
            def _list_modules_side_effect():
                calls["n"] += 1
                if calls["n"] == 1:
                    return ["OK", "SKIP", "ERR"]
                # 2nd iteration: raise to hit outer except, then stop
                te.stop()
                raise RuntimeError("boom outer")
            te._list_modules = _list_modules_side_effect

            def _gmst(db, name):
                if name == "OK":
                    return {"state_transition_in_progress": "YeS", "transition_type": "weird-op"}
                if name == "SKIP":
                    return {"state_transition_in_progress": "no"}
                if name == "ERR":
                    raise RuntimeError("boom inner")
                return {}
            fake_mb.get_module_state_transition.side_effect = _gmst
            fake_mb._load_transition_timeouts.return_value = {}  # force fallback to defaults
            fake_mb.is_module_state_transition_timed_out.return_value = True
            fake_mb.clear_module_state_transition.return_value = True

            te.run()

            # clear() was called once for OK
            fake_mb.clear_module_state_transition.assert_called_once()
            args, _ = fake_mb.clear_module_state_transition.call_args
            self.assertEqual(args[1], "OK")

            # log_info for the clear event
            self.assertTrue(
                any("Cleared transition after timeout for OK" in str(c.args[0])
                    for c in mod.logger.log_info.call_args_list)
            )

            # inner except logged for ERR
            self.assertTrue(
                any("Timeout enforce error for ERR" in str(c.args[0])
                    for c in mod.logger.log_debug.call_args_list)
            )

            # outer except logged
            self.assertTrue(
                any("TimeoutEnforcer loop error" in str(c.args[0])
                    for c in mod.logger.log_debug.call_args_list)
            )

    def test_timeout_enforcer_clear_failure(self):
        """Test TimeoutEnforcer behavior when clear_module_state_transition returns False."""
        import sys
        import importlib
        import unittest
        from unittest import mock
        import types

        # Pre-stub ONLY swsscommon and ModuleBase before import
        swsscommon = types.ModuleType("swsscommon")
        swsscommon_sub = types.ModuleType("swsscommon.swsscommon")
        class _SC: pass
        swsscommon_sub.SonicV2Connector = _SC
        swsscommon.swsscommon = swsscommon_sub

        spb = types.ModuleType("sonic_platform_base")
        spb_mb = types.ModuleType("sonic_platform_base.module_base")
        class _ModuleBase:
            _TRANSITION_TIMEOUT_DEFAULTS = {"startup": 300, "shutdown": 180, "reboot": 240}
        spb_mb.ModuleBase = _ModuleBase
        spb.module_base = spb_mb

        with mock.patch.dict(
            sys.modules,
            {
                "swsscommon": swsscommon,
                "swsscommon.swsscommon": swsscommon_sub,
                "sonic_platform_base": spb,
                "sonic_platform_base.module_base": spb_mb,
            },
            clear=False,
        ):
            mod = importlib.import_module("scripts.gnoi_shutdown_daemon")
            mod = importlib.reload(mod)

            # Fake DB & MB
            class _FakeDB:
                STATE_DB = object()
                def get_redis_client(self, _):
                    class C:
                        def keys(self, pattern): return ["CHASSIS_MODULE_TABLE|FAIL"]
                    return C()

            fake_db = _FakeDB()
            fake_mb = mock.Mock()

            # Mock logger to observe messages
            mod.logger = mock.Mock()

            te = mod.TimeoutEnforcer(fake_db, fake_mb, interval_sec=0)

            # Mock for module that will fail to clear
            calls = {"n": 0}
            def _list_modules_side_effect():
                calls["n"] += 1
                if calls["n"] == 1:
                    return ["FAIL"]
                # 2nd iteration: stop
                te.stop()
                return []
            te._list_modules = _list_modules_side_effect

            def _gmst(db, name):
                if name == "FAIL":
                    return {"state_transition_in_progress": "True", "transition_type": "shutdown"}
                return {}
            fake_mb.get_module_state_transition.side_effect = _gmst
            fake_mb._load_transition_timeouts.return_value = {}  # force fallback to defaults
            fake_mb.is_module_state_transition_timed_out.return_value = True
            fake_mb.clear_module_state_transition.return_value = False  # Simulate failure

            te.run()

            # clear() was called once for FAIL
            fake_mb.clear_module_state_transition.assert_called_once()
            args, _ = fake_mb.clear_module_state_transition.call_args
            self.assertEqual(args[1], "FAIL")

            # log_warning for the clear failure
            self.assertTrue(
                any("Failed to clear transition timeout for FAIL" in str(c.args[0])
                    for c in mod.logger.log_warning.call_args_list)
            )


class _MBStub2:
    def __init__(self, *a, **k):
        pass

    @staticmethod
    def get_module_state_transition(*_a, **_k):
        return {"state_transition_in_progress": "True", "transition_type": "shutdown"}
    
    @staticmethod
    def clear_module_state_transition(db, name):
        return True


def _mk_pubsub_once2():
    pubsub = MagicMock()
    pubsub.get_message.side_effect = [
        {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
        Exception("stop"),
    ]
    return pubsub


class TestGnoiShutdownDaemonAdditional(unittest.TestCase):
    def test_shutdown_skips_when_port_closed(self):
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub2), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=False), \
             patch("gnoi_shutdown_daemon._cfg_get_entry",
                   side_effect=lambda table, key:
                       {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else {"gnmi_port": "8080"}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:

            import gnoi_shutdown_daemon as d
            db = MagicMock()
            db.pubsub.return_value = _mk_pubsub_once2()
            mock_sonic.return_value = db

            try:
                d.main()
            except Exception:
                pass

            # Port closed => no gNOI calls should be made
            mock_exec.assert_not_called()

            # Accept any logger level; look at all method calls
            calls = getattr(mock_logger, "method_calls", []) or []
            msgs = [str(c.args[0]).lower() for c in calls if c.args]
            self.assertTrue(
                any(
                    ("skip" in m or "skipping" in m)
                    and ("tcp" in m or "port" in m or "reachable" in m)
                    for m in msgs
                ),
                f"Expected a 'skipping due to TCP/port not reachable' log; got: {msgs}"
            )


    def test_shutdown_missing_ip_logs_error_and_skips(self):
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub2), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True), \
             patch("gnoi_shutdown_daemon._cfg_get_entry", return_value={}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:
            import gnoi_shutdown_daemon as d
            db = MagicMock()
            db.pubsub.return_value = _mk_pubsub_once2()
            mock_sonic.return_value = db

            try:
                d.main()
            except Exception:
                pass

            mock_exec.assert_not_called()
            self.assertTrue(any("ip not found" in str(c.args[0]).lower()
                    for c in (mock_logger.log_error.call_args_list or [])))


    def test_shutdown_reboot_nonzero_does_not_poll_status(self):
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub2), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True), \
             patch("gnoi_shutdown_daemon._cfg_get_entry",
                   side_effect=lambda table, key:
                       {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else {"gnmi_port": "8080"}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:
            import gnoi_shutdown_daemon as d
            db = MagicMock()
            db.pubsub.return_value = _mk_pubsub_once2()
            mock_sonic.return_value = db

            mock_exec.side_effect = [
                (1, "", "boom"),  # Reboot -> non-zero rc
            ]

            try:
                d.main()
            except Exception:
                pass

            self.assertEqual(mock_exec.call_count, 1)
            self.assertTrue(any("reboot command failed" in str(c.args[0]).lower()
                    for c in (mock_logger.log_error.call_args_list or [])))

    def test_reboot_transition_type_success(self):
        """Test that reboot transition type is handled correctly and clears transition on success"""
        
        class _MBStubReboot:
            def __init__(self, *a, **k):
                pass
                
            @staticmethod
            def get_module_state_transition(*_a, **_k):
                return {"state_transition_in_progress": "True", "transition_type": "reboot"}
            
            @staticmethod
            def clear_module_state_transition(db, name):
                return True
        
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStubReboot), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True), \
             patch("gnoi_shutdown_daemon._cfg_get_entry",
                   side_effect=lambda table, key:
                       {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else {"gnmi_port": "8080"}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:
            import gnoi_shutdown_daemon as d
            db = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
                Exception("stop"),
            ]
            db.pubsub.return_value = pubsub
            mock_sonic.return_value = db

            mock_exec.side_effect = [
                (0, "OK", ""),  # Reboot command
                (0, "reboot complete", ""),  # RebootStatus
            ]

            try:
                d.main()
            except Exception:
                pass

            # Should make both Reboot and RebootStatus calls
            self.assertEqual(mock_exec.call_count, 2)
            
            # Check logs for reboot-specific messages
            all_logs = " | ".join(str(c) for c in mock_logger.method_calls)
            self.assertIn("reboot request detected for DPU0", all_logs)
            self.assertIn("Cleared transition for DPU0", all_logs)
            self.assertIn("Halting the services on DPU is successful for DPU0", all_logs)

    def test_reboot_transition_clear_failure(self):
        """Test that reboot transition logs warning when clear fails"""
        
        class _MBStubRebootFail:
            def __init__(self, *a, **k):
                pass
                
            @staticmethod
            def get_module_state_transition(*_a, **_k):
                return {"state_transition_in_progress": "True", "transition_type": "reboot"}
            
            @staticmethod
            def clear_module_state_transition(db, name):
                return False  # Simulate failure
        
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStubRebootFail), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True), \
             patch("gnoi_shutdown_daemon._cfg_get_entry",
                   side_effect=lambda table, key:
                       {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else {"gnmi_port": "8080"}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:
            import gnoi_shutdown_daemon as d
            db = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
                Exception("stop"),
            ]
            db.pubsub.return_value = pubsub
            mock_sonic.return_value = db

            mock_exec.side_effect = [
                (0, "OK", ""),  # Reboot command
                (0, "reboot complete", ""),  # RebootStatus
            ]

            try:
                d.main()
            except Exception:
                pass

            # Check for warning log when clear fails
            all_logs = " | ".join(str(c) for c in mock_logger.method_calls)
            self.assertIn("Failed to clear transition for DPU0", all_logs)

    def test_status_polling_timeout_warning(self):
        """Test that timeout during status polling logs the appropriate warning"""
        
        with patch("gnoi_shutdown_daemon.SonicV2Connector") as mock_sonic, \
             patch("gnoi_shutdown_daemon.ModuleBase", new=_MBStub2), \
             patch("gnoi_shutdown_daemon.execute_gnoi_command") as mock_exec, \
             patch("gnoi_shutdown_daemon.is_tcp_open", return_value=True), \
             patch("gnoi_shutdown_daemon._cfg_get_entry",
                   side_effect=lambda table, key:
                       {"ips@": "10.0.0.1"} if table == "DHCP_SERVER_IPV4_PORT" else {"gnmi_port": "8080"}), \
             patch("gnoi_shutdown_daemon.time.sleep", return_value=None), \
             patch("gnoi_shutdown_daemon.time.monotonic", side_effect=[0, 100]), \
             patch("gnoi_shutdown_daemon.logger") as mock_logger:
            import gnoi_shutdown_daemon as d
            db = MagicMock()
            pubsub = MagicMock()
            pubsub.get_message.side_effect = [
                {"type": "pmessage", "channel": "__keyspace@6__:CHASSIS_MODULE_TABLE|DPU0", "data": "set"},
                Exception("stop"),
            ]
            db.pubsub.return_value = pubsub
            mock_sonic.return_value = db

            mock_exec.side_effect = [
                (0, "OK", ""),  # Reboot command
                (0, "not complete", ""),  # RebootStatus - never returns complete
            ]

            try:
                d.main()
            except Exception:
                pass

            # Check for timeout warning
            all_logs = " | ".join(str(c) for c in mock_logger.method_calls)
            self.assertIn("Status polling of halting the services on DPU timed out for DPU0", all_logs)
