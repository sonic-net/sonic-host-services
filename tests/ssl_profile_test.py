"""Tests for ssl_profile host module (profile update -> rsyslog-config restart)."""

from unittest import mock

import pytest

from host_modules import ssl_profile
from host_modules.ssl_profile import MOD_NAME, SslProfile, register


PROFILE = 'siem-east'


@pytest.fixture
def tls_mod():
    with mock.patch.object(SslProfile, '__init__', return_value=None):
        yield SslProfile(MOD_NAME)


@pytest.fixture
def hooks(monkeypatch):
    used = mock.Mock(return_value=False)
    restart = mock.Mock()
    monkeypatch.setattr(ssl_profile, '_profile_used_by_syslog', used)
    monkeypatch.setattr(ssl_profile, '_restart_rsyslog_config', restart)
    return {'used': used, 'restart': restart}


class TestValidateProfileId:
    @pytest.mark.parametrize('profile_id', [
        '', None, '.', '..', 'a/b', 'a b', 'a;b', 'gnxi', 5])
    def test_rejects(self, profile_id):
        assert ssl_profile._validate_profile_id(profile_id) is not None

    @pytest.mark.parametrize('profile_id', ['siem-east', 'a.b_c-1', 'X'])
    def test_accepts(self, profile_id):
        assert ssl_profile._validate_profile_id(profile_id) is None


class TestNotifyProfileUpdate:
    def test_restarts_rsyslog_when_syslog_uses_profile(self, tls_mod, hooks):
        hooks['used'].return_value = True
        code, msg = tls_mod.notify_profile_update(PROFILE)
        assert code == 0, msg
        hooks['used'].assert_called_once_with(PROFILE)
        hooks['restart'].assert_called_once()

    def test_no_restart_when_profile_unused(self, tls_mod, hooks):
        code, msg = tls_mod.notify_profile_update(PROFILE)
        assert code == 0, msg
        hooks['restart'].assert_not_called()

    @pytest.mark.parametrize('profile_id', ['', '..', 'a/b', 'gnxi'])
    def test_invalid_id_does_nothing(self, tls_mod, hooks, profile_id):
        code, msg = tls_mod.notify_profile_update(profile_id)
        assert code == 1
        assert msg
        hooks['used'].assert_not_called()
        hooks['restart'].assert_not_called()


class TestProfileUsedBySyslog:
    def _db(self, table):
        db = mock.Mock()
        db.get_table.return_value = table
        return db

    def test_matches_tls_profile_id(self):
        db = self._db({
            '10.0.0.1': {'protocol': 'udp'},
            '10.0.0.5': {'protocol': 'tls', 'tls_profile_id': PROFILE},
        })
        with mock.patch('swsscommon.swsscommon.ConfigDBConnector',
                        return_value=db, create=True):
            assert ssl_profile._profile_used_by_syslog(PROFILE) is True
            assert ssl_profile._profile_used_by_syslog('other') is False

    def test_empty_profile_id(self):
        assert ssl_profile._profile_used_by_syslog('') is False

    def test_db_error_is_not_fatal(self):
        with mock.patch('swsscommon.swsscommon.ConfigDBConnector',
                        side_effect=RuntimeError('no db'), create=True):
            assert ssl_profile._profile_used_by_syslog(PROFILE) is False


class TestRestartRsyslogConfig:
    def test_runs_reset_failed_then_restart(self):
        with mock.patch('subprocess.run') as run:
            ssl_profile._restart_rsyslog_config()
        cmds = [c.args[0] for c in run.call_args_list]
        assert cmds == [
            ['systemctl', 'reset-failed', 'rsyslog-config', 'rsyslog'],
            ['systemctl', 'restart', 'rsyslog-config'],
        ]

    def test_failure_is_logged_not_raised(self):
        with mock.patch('subprocess.run', side_effect=OSError('boom')):
            ssl_profile._restart_rsyslog_config()


class TestNotifyConsumers:
    def test_only_restarts_syslog_when_used(self, hooks):
        ssl_profile._notify_consumers(PROFILE)
        hooks['restart'].assert_not_called()
        hooks['used'].return_value = True
        ssl_profile._notify_consumers(PROFILE)
        hooks['restart'].assert_called_once()


def test_register():
    assert register() == (SslProfile, MOD_NAME)
