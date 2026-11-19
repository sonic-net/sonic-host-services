"""SSL profile update notification for host TLS consumers.

TLS consumers on the host read their PEMs from one directory per SSL profile
id:

    /etc/sonic/ssl-certs/<profile_id>/{cert,key,ca-bundle}.pem

Whatever places or replaces those files (gNSI Certz, or an operator) calls
`notify_profile_update` afterwards. If a CONFIG_DB SYSLOG_SERVER row uses that
tls_profile_id, rsyslog-config is restarted so rsyslog.conf is re-rendered
and rsyslog reloads the new PEMs. Add future TLS consumers to
`_notify_consumers()`.

D-Bus name:

    org.SONiC.HostService.ssl_profile.notify_profile_update
"""

import logging
import re
import subprocess

from host_modules import host_service

MOD_NAME = 'ssl_profile'

DEFAULT_PROFILE = 'gnxi'
PROFILE_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')

logger = logging.getLogger(__name__)


def _validate_profile_id(profile_id):
    """Return an error string if profile_id is not usable, else None."""
    if not profile_id or not isinstance(profile_id, str):
        return 'profile id is empty'
    if profile_id in ('.', '..') or not PROFILE_ID_RE.match(profile_id):
        return 'invalid profile id %r' % (profile_id,)
    if profile_id == DEFAULT_PROFILE:
        return 'profile %r is the gNMI listener and is not a host SSL profile' % (
            profile_id,)
    return None


def _profile_used_by_syslog(profile_id):
    """True if CONFIG_DB SYSLOG_SERVER has a row with this tls_profile_id."""
    if not profile_id:
        return False
    try:
        from swsscommon.swsscommon import ConfigDBConnector
        db = ConfigDBConnector()
        db.connect(wait_for_init=False, retry_on=False)
        servers = db.get_table('SYSLOG_SERVER') or {}
        for row in servers.values():
            if isinstance(row, dict) and row.get('tls_profile_id') == profile_id:
                return True
    except Exception as err:
        logger.warning('%s: CONFIG_DB SYSLOG_SERVER lookup failed: %s',
                       MOD_NAME, err)
    return False


def _restart_rsyslog_config():
    """Restart rsyslog-config so Jinja re-scans /etc/sonic/ssl-certs."""
    try:
        subprocess.run(['systemctl', 'reset-failed', 'rsyslog-config',
                        'rsyslog'], check=False)
        subprocess.run(['systemctl', 'restart', 'rsyslog-config'], check=True)
    except Exception as err:
        logger.warning('%s: failed to restart rsyslog-config: %s', MOD_NAME, err)


def _notify_consumers(profile_id):
    """Reload host services that bind this SSL profile in CONFIG_DB.

    Add future TLS consumers here (RADIUS, OpenSSH, ...): check whether
    the profile id is referenced, then restart that consumer's config unit.
    """
    if _profile_used_by_syslog(profile_id):
        _restart_rsyslog_config()


class SslProfile(host_service.HostModule):
    """DBus endpoint that tells host TLS consumers a profile was updated."""

    @host_service.method(
        host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def notify_profile_update(self, profile_id):
        """Profile PEMs were installed, replaced or removed; reload consumers."""
        err = _validate_profile_id(profile_id)
        if err:
            return 1, err
        _notify_consumers(profile_id)
        return 0, 'Notified consumers of SSL profile %s' % profile_id


def register():
    """Return class name."""
    return SslProfile, MOD_NAME
