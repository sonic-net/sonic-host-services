"""Tests for debug_info."""

import importlib.util
import importlib.machinery
import os
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

def load_source(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, filename)
    spec = importlib.util.spec_from_file_location(modname, filename, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # The module is always executed and not cached in sys.modules.
    # Uncomment the following line to cache the module.
    sys.modules[module.__name__] = module
    loader.exec_module(module)
    return module

load_source("host_service", host_modules_path + "/host_service.py")
load_source("debug_info", host_modules_path + "/debug_info.py")

from debug_info import *

class TestDebugArtifactCollector:
    @classmethod
    def setup_class(cls):
        with mock.patch("debug_info.super"):
            cls.debug_info_module = DebugArtifactCollector(MOD_NAME)

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
    
    def test_get_device_metadata_failure(self):
        with mock.patch("debug_info.swsscommon.SonicV2Connector") as mock_conn, \
            mock.patch("debug_info.logger.warning") as mock_warn:
            instance = mock_conn.return_value
            instance.get_all.side_effect = Exception("db error")
            hostname, board_type = self.debug_info_module.get_device_metadata()
            assert hostname == "switch"
            assert board_type == ""
            mock_warn.assert_called_with("Failed to read hostname/board_type from CONFIG_DB: db error")

    def test_get_device_metadata_success(self):
        with mock.patch("debug_info.swsscommon.SonicV2Connector") as mock_conn:
            instance = mock_conn.return_value
            instance.get_all.return_value = {
                "hostname": "sonic-host",
                "platform": "x86_64-kvm_x86_64-r0"
                }
            hostname, board_type = self.debug_info_module.get_device_metadata()
            assert hostname == "sonic-host"
            assert board_type == "x86_64-kvm_x86_64-r0"

    def test_get_device_metadata_missing_fields(self):
        with mock.patch("debug_info.swsscommon.SonicV2Connector") as mock_conn:
            instance = mock_conn.return_value
            instance.get_all.return_value = {}
            hostname, board_type = self.debug_info_module.get_device_metadata()
            assert hostname == "switch"
            assert board_type == ""

    def test_collect_invalid_json(self):
        rc, msg = self.debug_info_module.collect("not a json")
        assert rc == 1
        assert "invalid input" in msg

    def test_collect_success(self):
        rc, msg = self.debug_info_module.collect("{}")
        assert rc == 0
        assert msg.startswith(ARTIFACT_DIR)

    def test_collect_artifact_throw_exception(self):
        with mock.patch("debug_info.DebugArtifactCollector.collect_artifacts", side_effect=Exception("bad")):
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
        assert cls is DebugArtifactCollector
        assert modname == "debug_info"

    def test_collect_counter_artifacts_with_errors(self, tmp_path):
        fake_dir = tmp_path
        with mock.patch("debug_info.DebugArtifactCollector._run_command", side_effect=[(1, "", "err"), (1, "", "err")]):
            DebugArtifactCollector._collect_counter_artifacts(str(fake_dir), "ut_", "x86_64")

    def test_save_persistent_storage_flag_exists(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        with open(fake_flag, "w") as f:
            f.write("")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag):
            DebugArtifactCollector._save_persistent_storage("artifact")
        assert os.path.exists(fake_flag)

    def test_save_persistent_oserror_creating_flag(self):
        bad_flag = os.path.join(self.base_path, "bad", "nv.tmp")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", bad_flag):
            DebugArtifactCollector._save_persistent_storage("artifact")

    def test_save_persistent_path_not_exists(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag), \
             mock.patch("debug_info.ARTIFACT_DIR", self.base_path), \
             mock.patch("os.path.getsize", side_effect=OSError):
            DebugArtifactCollector._save_persistent_storage("artifact.tar")

    def test_save_persistent_not_enough_space(self):
        fake_flag = os.path.join(self.base_path, "nv.tmp")
        artifact = os.path.join(self.base_path, "artifact.tar")
        with open(artifact, "w") as f:
            f.write("data")

        with mock.patch("debug_info.NONVOLATILE_TMP_FLAG", fake_flag), \
             mock.patch("debug_info.ARTIFACT_DIR", self.base_path), \
             mock.patch("os.path.getsize", return_value=100), \
             mock.patch("shutil.disk_usage", return_value=(0, 0, 0)):
            DebugArtifactCollector._save_persistent_storage("artifact.tar")
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
             mock.patch("debug_info.DebugArtifactCollector._run_command", return_value=(0, "", "")):
            DebugArtifactCollector._save_persistent_storage("artifact.tar")
        assert os.path.exists(fake_flag)

    def test_collect_teamdctl_get_portchannels_fails(self):
        with mock.patch("debug_info.SonicDbUtils.get_portchannels",
                        side_effect=Exception("DB error")), \
             mock.patch("debug_info.logger.warning") as mock_warn:
            DebugArtifactCollector._collect_teamdctl_data(self.base_path)
        mock_warn.assert_called()

    def test_collect_teamdctl_no_portchannels(self):
        with mock.patch("debug_info.SonicDbUtils.get_portchannels",return_value=[]), \
             mock.patch("debug_info.logger.warning") as mock_warn:
            DebugArtifactCollector._collect_teamdctl_data(self.base_path)
        # No files created
        assert os.listdir(self.base_path) == []
        mock_warn.assert_called_with("teamdctl: No PortChannels found, skipping teamdctl collection")

    def test_teamdctl_valid_portchannel_success(self):
        with mock.patch("debug_info.SonicDbUtils.get_portchannels",return_value=["PortChannel01"]), \
             mock.patch("debug_info.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="teamdctl output",
                stderr=""
            )
            DebugArtifactCollector._collect_teamdctl_data(self.base_path)
        filepath = os.path.join(self.base_path, "teamdctl_PortChannel01.txt")
        assert os.path.exists(filepath)
        with open(filepath) as f:
            assert "teamdctl output" in f.read()
    
    def test_teamdctl_valid_portchannel_failure(self):
        with mock.patch("debug_info.SonicDbUtils.get_portchannels",return_value=["PortChannel02"]), \
             mock.patch("debug_info.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="some error"
            )
            DebugArtifactCollector._collect_teamdctl_data(self.base_path)
        filepath = os.path.join(self.base_path, "teamdctl_PortChannel02.txt")
        assert not os.path.exists(filepath)

    def test_teamdctl_file_write_failure(self):
        with mock.patch("debug_info.SonicDbUtils.get_portchannels",
                        return_value=["PortChannel03"]), \
             mock.patch("debug_info.subprocess.run") as mock_run, \
             mock.patch("builtins.open",side_effect=FileNotFoundError), \
             mock.patch("debug_info.logger.warning") as mock_warn:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="teamdctl data",
                stderr=""
            )
            DebugArtifactCollector._collect_teamdctl_data(self.base_path)
        mock_warn.assert_called()
