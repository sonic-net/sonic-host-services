"""Tests for debug_info."""

import imp
import os
import sys
import logging
import itertools
import tempfile

if sys.version_info >= (3, 3):
    from unittest import mock
else:
    import mock

test_path = os.path.dirname(os.path.abspath(__file__))
sonic_host_service_path = os.path.dirname(test_path)
host_modules_path = os.path.join(sonic_host_service_path, "host_modules")
sys.path.insert(0, sonic_host_service_path)

imp.load_source("host_service", host_modules_path + "/host_service.py")
imp.load_source("debug_info", host_modules_path + "/debug_info.py")
dummy_artifact_path = "/tmp/dummy_healthz_artifact.tar.gz"

from debug_info import *

class TestDebugInfo:
    @classmethod
    def setup_class(cls):
        with mock.patch("debug_info.super"):
            cls.debug_info_module = DebugInfo(MOD_NAME)

    def setup_method(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = self.tmpdir.name

    def teardown_method(self):
        self.tmpdir.cleanup()

    def test_run_command_success(self):
        with mock.patch("debug_info.subprocess.Popen") as mp:
            proc = mock.Mock()
            proc.communicate.return_value = ("out", "err")
            proc.returncode = 0
            mp.return_value = proc
            rc, out, err = self.debug_info_module._run_command("echo test")
            assert rc == 0
            assert out == "out"
            assert err == "err"

    def test_run_command_timeout(self):
        with mock.patch("debug_info.subprocess.Popen") as mp:
            proc = mock.Mock()
            proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="sleep 5", timeout=1)
            proc.kill = mock.Mock()
            mp.return_value = proc
            rc, out, err = self.debug_info_module._run_command("sleep 5", timeout =1)
            assert rc == 1
            assert "timeout" in out

    def test_get_board_type_failure(self):
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(1, "", "err")):
            bt = self.debug_info_module.get_board_type()
            assert bt == ""

    def test_get_board_type_success(self):
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "x86_64", "")):
            bt = self.debug_info_module.get_board_type()
            assert bt == "x86_64"


    def test_get_hostname_failure(self):
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(1, "", "err")):
            hn = self.debug_info_module.get_hostname()
            assert hn == "switch"


    def test_get_hostname_success(self):
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "sonic-host", "")):
            hn = self.debug_info_module.get_hostname()
            assert hn == "sonic-host"

    def test_collect_invalid_json(self):
        rc, msg = self.debug_info_module.collect("not a json")
        assert rc == 1
        assert "invalid input" in msg

    def test_collect_already_running(self):
        with mock.patch("debug_info.threading.Thread") as mth:
            mthread = mock.Mock()
            mth.return_value = mthread
            rc, msg = self.debug_info_module.collect("[]")
            assert rc == 0
            assert msg.startswith(ARTIFACT_DIR)
            mthread.start.assert_called_once()

    def test_collect_thread_runtime_error(self):
        with mock.patch("debug_info.threading.Thread", side_effect=RuntimeError("bad")):
            rc, msg = self.debug_info_module.collect("[]")
            assert rc == 1
            assert "Previous artifact collection is ongoing" in msg

    def test_check_not_ready(self):
        self.debug_info_module._ongoing_thread = "artifact"
        rc, msg = self.debug_info_module.check("artifact")
        assert rc == 1
        assert "artifact not ready" in msg.lower()

    def test_check_ready(self):
        self.debug_info_module._ongoing_thread = "something"
        rc, msg = self.debug_info_module.check("artifact")
        assert rc == 0
        assert msg == ""
    
    def test_ack_failure(self):
        with mock.patch("debug_info.os.remove", side_effect=OSError("fail")):
            rc, msg = self.debug_info_module.ack("/tmp/foo")
            assert rc == 1
            assert "Failed to delete" in msg

    def test_ack_success(self, tmp_path):
        f = tmp_path / "foo.tar.gz"
        f.write_text("dummy")
        artifact_path = str(f)
        with mock.patch("debug_info.os.remove") as mrm, \
             mock.patch.object(self.debug_info_module, "_update_db", return_value=None):
            rc, msg = self.debug_info_module.ack(str(f))
            mrm.assert_called_once()
            assert rc == 0

    def test_register(self):
        cls, modname = register()
        assert cls is DebugInfo
        assert modname == "debug_info"

    def test_sanitize_filename(self):
        filename = "bad'name; file/with|chars"
        sanitized = DebugInfo._sanitize_filename(filename)
        assert "'" not in sanitized
        assert ";" not in sanitized
        assert " " not in sanitized
        assert "/" not in sanitized
        assert "'" not in sanitized
        assert "|" in sanitized

    def test_update_db_calls_clear_alarm(self):
        with mock.patch.object(self.debug_info_module,"_clear_alarm") as mclear:
            self.debug_info_module._update_db()
            mclear.assert_called_once()
    
    def test_collect_counter_artifacts_success(self, tmp_path):
        fake_dir = tmp_path
        fake_prefix = "ut_"
        with mock.patch("debug_info.COUNTER_CMDS", ["echo test > {0}/file"]), \
             mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "ok", "")) as mrun, \
             mock.patch("debug_info.get_sdk_files", return_value=["/proc/bcm/test_stats"]):
            DebugInfo._collect_counter_artifacts(str(fake_dir), fake_prefix, "x86_64")
            # should have run both COUNTER_CMDS and sdk_file cat command
            assert mrun.call_count >= 2

    def test_collect_counter_artifacts_with_errors(self, tmp_path):
        fake_dir = tmp_path
        with mock.patch("debug_info.COUNTER_CMDS", ["badcmd {0}"]), \
             mock.patch("debug_info.DebugInfo._run_command", side_effect=[(1, "", "err"), (1, "", "err")]), \
             mock.patch("debug_info.get_sdk_files", return_value=["/proc/bcm/badfile"]):
            # Should still complete despite errors
            DebugInfo._collect_counter_artifacts(str(fake_dir), "ut_", "x86_64")

    def test_send_component_request_all(self, tmp_path):
        fake_producer = mock.Mock()
        with mock.patch("debug_info.os.makedirs") as mmkdir, \
             mock.patch("debug_info.swsscommon.FieldValuePairs", return_value="fvp"):
            comps = DebugInfo._send_component_request(COMPONENT_ALL, "dirname", "critical", fake_producer)
            # Should contain all COMPONENTS
            assert set(comps.keys()) == set(COMPONENTS)
            fake_producer.send.assert_any_call(COMPONENTS[0], mock.ANY, "fvp")
            fake_producer.send.assert_any_call(COMPONENTS[1], mock.ANY, "fvp")
            assert mmkdir.call_count == len(COMPONENTS)

    def test_send_component_request_single(self):
        fake_producer = mock.Mock()
        with mock.patch("debug_info.os.makedirs"), \
             mock.patch("debug_info.swsscommon.FieldValuePairs", return_value="fvp"):
            comps = DebugInfo._send_component_request("orch", "dirname", "alert", fake_producer)
            assert list(comps.keys()) == ["orch"]
            fake_producer.send.assert_called_once()

    def test_get_component_response_invalid(self):
        # Fake components dict
        comps = {"orch": "path1"}
        fake_sel = mock.Mock()
        fake_consumer = mock.Mock()

        fake_sel.select.side_effect = itertools.repeat((swsscommon.Select.OBJECT, None))
        fake_consumer.pop.return_value = ("orch", "data", [("key1", "val1")])
        
        with mock.patch("debug_info.DebugInfo._validate_component_response", return_value=False):
            resp = DebugInfo._get_component_response(time.time() + 1, comps, fake_sel, fake_consumer)
            # Component dict should be emptied
            assert resp is None
    
    def test_get_component_response_valid(self):
        # Fake components dict
        comps = {"orch": "path1"}
        fake_sel = mock.Mock()
        fake_consumer = mock.Mock()

        fake_sel.select.side_effect = itertools.repeat((swsscommon.Select.OBJECT, None))
        fake_consumer.pop.return_value = ("orch", "data", [("key1", "val1")])
        
        with mock.patch("debug_info.DebugInfo._validate_component_response", return_value=True):
            DebugInfo._get_component_response(time.time() + 1, comps, fake_sel, fake_consumer)
            # Component dict should be emptied
            assert not comps, f"Expected all components collected but got {comps}"

    def test_get_component_response_timeout(self):
        comps = {"orch": "path1"}
        fake_sel = mock.Mock()
        fake_consumer = mock.Mock()

        # First TIMEOUT, then OTHER, then OBJECT but invalid response
        fake_sel.select.side_effect = itertools.chain([("TIMEOUT", None), ("OTHER", None)], \
                                                       itertools.repeat(("OBJECT", None)))
        fake_consumer.pop.return_value = ("orch", "data", "fvs")

        with mock.patch("debug_info.swsscommon.Select.TIMEOUT", "TIMEOUT"), \
             mock.patch("debug_info.swsscommon.Select.OBJECT", "OBJECT"), \
             mock.patch("debug_info.DebugInfo._validate_component_response", return_value=False):
            DebugInfo._get_component_response(time.time() + 1, comps, fake_sel, fake_consumer)
            # "orch" still left because validation failed
            assert "orch" in comps

    def test_validate_component_response_Success(self):
        comps = {"orch": "path1"}
        op, data, fvs = "orch", "path1", [("status", "success"), ("err_str", "")]
        assert DebugInfo._validate_component_response(comps, op, data, fvs) is True


    def test_validate_component_response_invalid_component(self):
        comps = {"orch": "path1"}
        op, data, fvs = "invalid", "path1", [("status", "success"), ("err_str", "")]
        result = DebugInfo._validate_component_response(comps, op, data, fvs)
        assert result is False

    def test_validate_component_response_directory_mismatch(self):
        comps = {"orch": "path1"}
        op, data, fvs = "orch", "wrong_path", [("status", "success"), ("err_str", "")]
        result = DebugInfo._validate_component_response(comps, op, data, fvs)
        assert result is False


    def test_validate_component_response_missing_err_str(self):
        comps = {"orch": "path1"}
        op, data, fvs = "orch", "path1", [("status", "success")]
        result = DebugInfo._validate_component_response(comps, op, data, fvs)
        assert result is False

    def test_validate_component_response_missing_status(self):
        comps = {"orch": "path1"}
        op, data, fvs = "orch", "path1", [ ("err_str", "no_error")]
        result = DebugInfo._validate_component_response(comps, op, data, fvs)
        assert result is False

    def test_validate_component_response_failure_status(self):
        comps = {"orch": "path1"}
        op, data, fvs = "orch", "path1", [("status", "failed"), ("err_str", "something failed")]
        result = DebugInfo._validate_component_response(comps, op, data, fvs)
        assert result is True

    def test_save_persistent_storage_flag_exists(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        with open(fake_flag, "w") as f:
            f.write("")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag):
            DebugInfo._save_persistent_storage("artifact")
        assert os.path.exists(fake_flag)

    def test_save_persistent_oserror_creating_flag(self):
        bad_flag = os.path.join(self.base_path, "bad", "nv.tmp")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", bad_flag):
            DebugInfo._save_persistent_storage("artifact")

    def test_save_persistent_path_not_exists(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag), \
             mock.patch("debug_info.ARTIFACT_DIR", self.base_path), \
             mock.patch("os.path.getsize", side_effect=OSError):
            DebugInfo._save_persistent_storage("artifact.tar")

    def test_save_persistent_not_enough_space(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        artifact = os.path.join(self.base_path, "artifact.tar")
        with open(artifact, "w") as f:
            f.write("data")

        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag), \
             mock.patch("debug_info.ARTIFACT_DIR", self.base_path), \
             mock.patch("os.path.getsize", return_value=100), \
             mock.patch("shutil.disk_usage", return_value=(0, 0, 0)):
            DebugInfo._save_persistent_storage("artifact.tar")
        assert os.path.exists(fake_flag)


    def test_save_persistent_storage_success(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        artifact = os.path.join(self.base_path, "artifact.tar")
        with open(artifact, "w") as f:
            f.write("data")

        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag), \
             mock.patch("debug_info.ARTIFACT_DIR", self.base_path), \
             mock.patch("os.path.getsize", return_value=100), \
             mock.patch("shutil.disk_usage", return_value=(0, 1000, 1000)), \
             mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "", "")):
            DebugInfo._save_persistent_storage("artifact.tar")
        assert os.path.exists(fake_flag)

    def test_collect_teamdctl_redis_command_fails(self):
        with mock.patch("debug_info.subprocess.run",
                        side_effect=subprocess.CalledProcessError(1, "redis")):
            DebugInfo._collect_teamdctl_data(self.base_path)

    def test_collect_teamdctl_empty_trunks(self):
        fake_result = mock.Mock()
        fake_result.stdout = ""
        with mock.patch("debug_info.subprocess.run", return_value=fake_result):
            DebugInfo._collect_teamdctl_data(self.base_path)
        assert len(os.listdir(self.base_path)) == 0

    def test_teamdctl_valid_trunk_success(self):
        redis_result = mock.Mock(stdout="id|PortChannel01\n")
        teamdctl_result = mock.Mock(returncode=0, stdout="teamdctl output")
 
        def fake_run(cmd, **kwargs):
            if "redis" in cmd:
                return redis_result
            return teamdctl_result
 
        with mock.patch("debug_info.subprocess.run", side_effect=fake_run):
            DebugInfo._collect_teamdctl_data(self.base_path)
 
        filepath = os.path.join(self.base_path, "teamdctl_PortChannel01.txt")
        assert os.path.exists(filepath)
        with open(filepath) as f:
            assert "teamdctl output" in f.read()
 
    def test_teamdctl_valid_trunk_failure(self):
        redis_result = mock.Mock(stdout="id|PortChannel02\n")
        teamdctl_result = mock.Mock(returncode=1, stderr="some error")
 
        def fake_run(cmd, **kwargs):
            if "redis" in cmd:
                return redis_result
            return teamdctl_result
 
        with mock.patch("debug_info.subprocess.run", side_effect=fake_run):
            DebugInfo._collect_teamdctl_data(self.base_path)
        # File should not exist
        filepath = os.path.join(self.base_path, "teamdctl_PortChannel02.txt")
        assert not os.path.exists(filepath)
 
    def test_teamdctl_file_write_failure(self):
        redis_result = mock.Mock(stdout="id|PortChannel03\n")
        teamdctl_result = mock.Mock(returncode=0, stdout="teamdctl data")
 
        def fake_open(*args, **kwargs):
            raise FileNotFoundError
 
        def fake_run(cmd, **kwargs):
            if "redis" in cmd:
                return redis_result
            return teamdctl_result
 
        with mock.patch("debug_info.subprocess.run", side_effect=fake_run), \
             mock.patch("builtins.open", side_effect=fake_open):
            DebugInfo._collect_teamdctl_data(self.base_path)


    @mock.patch("debug_info.swsscommon")
    @mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "ok", ""))
    @mock.patch("debug_info.DebugInfo._send_component_request", return_value={"orch": "path"})
    @mock.patch("debug_info.DebugInfo._get_component_response", return_value={"orch": {}})
    @mock.patch("debug_info.DebugInfo._collect_teamdctl_data")
    @mock.patch("debug_info.DebugInfo._collect_counter_artifacts")
    def test_collect_artifacts_valid(
        self, mock_counter, mock_teamd, mock_resp, mock_send, mock_run, mock_swss
    ):
        req = json.dumps({"component": "orch", "log_level": "ALERT"})
        rc, out = collect_artifacts(req, "1234", "boardX", "hostA")
 
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(".tar.gz"))
        self.assertTrue(os.path.exists(out))
 
        # mocks were triggered
        mock_send.assert_called_once()
        mock_resp.assert_called_once()
        mock_teamd.assert_called_once()
        mock_counter.assert_called()
 
    def test_collect_artifacts_invalid_json(self):
        req = "NOT_JSON"
        rc, msg = collect_artifacts(req, "1234", "boardX", "hostA")
        self.assertEqual(rc, 1)
        self.assertIn("invalid input", msg)
 
    @mock.patch("debug_info.swsscommon")
    @mock.patch("debug_info.DebugInfo._send_component_request", side_effect=Exception("boom"))
    @mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "ok", ""))
    @mock.patch("debug_info.DebugInfo._get_component_response", return_value={})
    @mock.patch("debug_info.DebugInfo._collect_teamdctl_data")
    def test_collect_artifacts_send_component_exception(
        self, mock_teamd, mock_resp, mock_run, mock_send, mock_swss
    ):
        req = json.dumps({"component": "orch"})
        rc, out = collect_artifacts(req, "1234", "boardX", "hostA")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(".tar.gz"))
 
    @mock.patch("debug_info.swsscommon")
    @mock.patch("debug_info.DebugInfo._send_component_request", return_value={})
    @mock.patch("debug_info.DebugInfo._get_component_response", return_value={})
    @mock.patch("debug_info.DebugInfo._collect_teamdctl_data")
    @mock.patch("debug_info.DebugInfo._run_command", return_value=(1, "", "err"))
    def test_collect_artifacts_run_command_fail(
        self, mock_run, mock_teamd, mock_resp, mock_send, mock_swss
    ):
        req = json.dumps({"component": "orch"})
        rc, out = collect_artifacts(req, "1234", "boardX", "hostA")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(".tar.gz"))
 
    @mock.patch("debug_info.swsscommon")
    @mock.patch("debug_info.DebugInfo._send_component_request", return_value={})
    @mock.patch("debug_info.DebugInfo._get_component_response", return_value={})
    @mock.patch("debug_info.DebugInfo._collect_teamdctl_data")
    @mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "ok", ""))
    @mock.patch("debug_info.DebugInfo._save_persistent_storage")
    def test_collect_artifacts_with_persistent_storage(
        self, mock_save, mock_run, mock_teamd, mock_resp, mock_send, mock_swss
    ):
        req = json.dumps({"component": "orch", "persistent_storage": True})
        rc, out = collect_artifacts(req, "1234", "boardX", "hostA")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith(".tar.gz"))
        mock_save.assert_called_once()
