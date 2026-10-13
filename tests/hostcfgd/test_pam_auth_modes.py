"""
Regression tests for F058: ensure no pam_unix.so authentication entry in
the rendered common-auth-sonic PAM stack permits empty ("nullok") passwords,
across every supported AAA['authentication']['login'] mode, while preserving
local/remote fallback ordering and existing failthrough behavior.
"""
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

from tests.hostcfgd.test_tacacs_authentication import (
    authenticate,
    pam_auth_lines,
)

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "templates"
    / "common-auth-sonic.j2"
)
TEMPLATES_DIR = TEMPLATE_PATH.parent

# Every auth['login'] value explicitly handled by common-auth-sonic.j2,
# plus "unspecified" to exercise the {% else %} default/fallback branch.
ALL_LOGIN_MODES = [
    "local",
    "local,tacacs+",
    "tacacs+",
    "tacacs+,local",
    "local,radius",
    "radius,local",
    "radius",
    "ldap,local",
    "local,ldap",
    "ldap",
    "unspecified",
]

# Modes that must retain a pam_unix.so local-authentication fallback line.
MODES_WITH_LOCAL_FALLBACK = [
    "local",
    "local,tacacs+",
    "tacacs+",
    "tacacs+,local",
    "local,radius",
    "radius,local",
    "radius",
    "ldap,local",
    "local,ldap",
    "unspecified",
]

# Pure "ldap" has no local fallback; this is pre-existing, intentional
# behavior and must not be changed by this fix.
MODES_WITHOUT_LOCAL_FALLBACK = ["ldap"]

# Modes where the local pam_unix.so entry comes before any remote module.
# ("local" alone has no remote module, so it is not part of this ordering set.)
LOCAL_FIRST_MODES = ["local,tacacs+", "local,radius", "local,ldap"]

# Modes where a remote module is attempted before the local pam_unix.so
# fallback (remote-first / local-last).
REMOTE_FIRST_MODES = [
    "tacacs+",
    "tacacs+,local",
    "radius,local",
    "radius",
    "ldap,local",
]


def _tacacs_servers(count):
    return [
        SimpleNamespace(
            ip="192.0.2.{}".format(10 + index),
            tcp_port=49,
            passkey="test-passkey",
            auth_type="pap",
            timeout=5,
            vrf=None,
        )
        for index in range(count)
    ]


def _radius_servers(count):
    return [
        SimpleNamespace(
            ip="198.51.100.{}".format(10 + index),
            auth_port=1812,
            auth_type="pap",
            retransmit=3,
            statistics=False,
            skip_msg_auth=False,
        )
        for index in range(count)
    ]


def render_pam_stack(login, failthrough=False, server_count=1):
    """Render common-auth-sonic.j2 for the given login mode."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        trim_blocks=True,
    )
    env.filters["sub"] = lambda values, start, end: values[start:end]
    template = env.get_template(TEMPLATE_PATH.name)

    if "tacacs+" in login:
        servers = _tacacs_servers(server_count)
    elif "radius" in login:
        servers = _radius_servers(server_count)
    else:
        servers = []

    return template.render(
        auth={"login": login, "failthrough": failthrough},
        servers=servers,
        src_ip=None,
    )


@pytest.mark.parametrize("login", ALL_LOGIN_MODES)
def test_no_pam_unix_nullok_for_any_login_mode(login):
    """No pam_unix.so authentication entry may permit empty passwords."""
    lines = pam_auth_lines(render_pam_stack(login))
    pam_unix_lines = [line for line in lines if "pam_unix.so" in line]

    if login not in MODES_WITHOUT_LOCAL_FALLBACK:
        assert pam_unix_lines, "Expected at least one pam_unix.so line for {}".format(login)
    for line in pam_unix_lines:
        assert "nullok" not in line, "nullok found in: {}".format(line)
        # try_first_pass must be preserved so credentials aren't re-prompted.
        assert "try_first_pass" in line


@pytest.mark.parametrize("login", MODES_WITH_LOCAL_FALLBACK)
def test_required_local_fallback_entries_still_exist(login):
    lines = pam_auth_lines(render_pam_stack(login))
    assert any("pam_unix.so" in line for line in lines), (
        "Expected a local pam_unix.so fallback entry for login={}".format(login)
    )


@pytest.mark.parametrize("login", MODES_WITHOUT_LOCAL_FALLBACK)
def test_pure_ldap_has_no_local_fallback(login):
    # Pre-existing behavior: "ldap" alone has no pam_unix.so fallback.
    # This fix must not add or remove fallback modules, only strip nullok.
    lines = pam_auth_lines(render_pam_stack(login))
    assert not any("pam_unix.so" in line for line in lines)


@pytest.mark.parametrize("login", LOCAL_FIRST_MODES)
def test_local_first_ordering_is_unchanged(login):
    lines = pam_auth_lines(render_pam_stack(login))
    local_index = next(i for i, line in enumerate(lines) if "pam_unix.so" in line)
    remote_indexes = [
        i
        for i, line in enumerate(lines)
        if "pam_tacplus.so" in line
        or "pam_radius_auth.so" in line
        or "pam_ldap.so" in line
    ]
    assert remote_indexes, "Expected at least one remote module for {}".format(login)
    assert all(local_index < remote_index for remote_index in remote_indexes)


@pytest.mark.parametrize("login", REMOTE_FIRST_MODES)
def test_remote_first_ordering_is_unchanged(login):
    lines = pam_auth_lines(render_pam_stack(login, server_count=1))
    local_index = next(i for i, line in enumerate(lines) if "pam_unix.so" in line)
    remote_indexes = [
        i
        for i, line in enumerate(lines)
        if "pam_tacplus.so" in line
        or "pam_radius_auth.so" in line
        or "pam_ldap.so" in line
    ]
    assert remote_indexes, "Expected at least one remote module for {}".format(login)
    assert all(remote_index < local_index for remote_index in remote_indexes)


@pytest.mark.parametrize("password_valid", [True, False])
@pytest.mark.parametrize("login", ["local", "unspecified"])
def test_local_only_authentication_behavior(login, password_valid):
    """A valid local password succeeds; an incorrect/empty one fails."""
    lines = pam_auth_lines(render_pam_stack(login))
    assert authenticate(
        lines, tacacs_result="PAM_SUCCESS", password_valid=password_valid
    ) is password_valid


@pytest.mark.parametrize("server_count", [0, 1, 3])
def test_tacacs_local_first_numeric_jumps_preserved(server_count):
    """local,tacacs+ keeps local-first with valid local password succeeding
    regardless of remote server count."""
    lines = pam_auth_lines(
        render_pam_stack("local,tacacs+", server_count=server_count)
    )
    assert authenticate(
        lines, tacacs_result="PAM_AUTH_ERR", password_valid=True
    )


def test_no_nullok_remains_in_production_pam_template():
    """Repository-wide static guard: no pam_unix.so authentication entry in
    any production PAM template (not just common-auth-sonic.j2) may permit
    empty ("nullok") passwords."""
    violations = []
    for template_path in sorted(TEMPLATES_DIR.glob("*.j2")):
        for line in template_path.read_text().splitlines():
            if "pam_unix.so" in line and "nullok" in line:
                violations.append("{}: {}".format(template_path.name, line))
    assert not violations, (
        "Found disallowed nullok in production PAM template(s):\n"
        + "\n".join(violations)
    )
