"""Tests for debug_info."""

import imp
import itertools
import json
import logging
import os
import shutil
import sys
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
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(1, "", "err")) as mock_run, \
            mock.patch("debug_info.logger.warning") as mock_warn:
            bt = self.debug_info_module.get_board_type()
            mock_run.assert_called_once_with(BOARD_TYPE_CMD)
            mock_warn.assert_called_with("fail to execute command '%s': %s", BOARD_TYPE_CMD, "err")
            assert bt == ""

    def test_get_board_type_success(self):
        with mock.patch("debug_info.DebugInfo._run_command", return_value=(0, "x86_64-kvm_x86_64-r0", "")):
            bt = self.debug_info_module.get_board_type()
            assert bt == "x86_64-kvm_x86_64-r0"

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

    def test_collect_success(self):
        rc, msg = self.debug_info_module.collect("{}")
        assert rc == 0
        assert msg.startswith(ARTIFACT_DIR)

    def test_collect_artifact_throw_exception(self):
        with mock.patch("debug_info.DebugInfo.collect_artifacts", side_effect=Exception("bad")):
            rc, msg = self.debug_info_module.collect("[]")
            assert rc == 1
            assert "Artifact collection failed" in msg

    def test_check_always_ready(self):
        rc, msg = self.debug_info_module.check("anything")
        assert rc == 0
        assert "ready" in msg.lower()
    
    def test_ack_failure(self):
        with mock.patch("debug_info.os.remove", side_effect=OSError("fail")):
            rc, msg = self.debug_info_module.ack("/tmp/foo")
            assert rc == 1
            assert "Failed to delete" in msg
    
    def test_register(self):
        cls, modname = register()
        assert cls is DebugInfo
        assert modname == "debug_info"

    def test_collect_counter_artifacts_with_errors(self, tmp_path):
        fake_dir = tmp_path
        with mock.patch("debug_info.COUNTER_CMDS", ["badcmd {0}"]), \
             mock.patch("debug_info.DebugInfo._run_command", side_effect=[(1, "", "err"), (1, "", "err")]):
            DebugInfo._collect_counter_artifacts(str(fake_dir), "ut_", "x86_64")

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

