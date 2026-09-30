from pathlib import Path
import re
from types import SimpleNamespace

import jinja2
import pytest


TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "templates"
    / "common-auth-sonic.j2"
)


def render_authentication_config(
    login,
    failthrough=False,
    server_count=1,
    vrf=None,
    src_ip=None,
    servers=None,
):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        trim_blocks=True,
    )
    env.filters["sub"] = lambda values, start, end: values[start:end]
    template = env.get_template(TEMPLATE_PATH.name)
    if servers is None:
        servers = [
            SimpleNamespace(
                ip="192.0.2.{}".format(index + 10),
                tcp_port=49,
                passkey="test-passkey",
                auth_type="pap",
                timeout=5,
                vrf=vrf,
            )
            for index in range(server_count)
        ]

    return template.render(
        auth={"login": login, "failthrough": failthrough},
        servers=servers,
        src_ip=src_ip,
    )


def pam_auth_lines(rendered_config):
    return [
        line.strip()
        for line in rendered_config.splitlines()
        if line.strip().startswith("auth")
    ]


def authenticate(lines, tacacs_result, password_valid):
    result_name = {
        "PAM_SUCCESS": "success",
        "PAM_AUTH_ERR": "auth_err",
        "PAM_SERVICE_ERR": "service_err",
        "PAM_AUTHINFO_UNAVAIL": "authinfo_unavail",
    }
    index = 0
    while index < len(lines):
        line = lines[index]
        if "pam_tacplus.so" in line:
            result = tacacs_result
        elif "pam_unix.so" in line:
            result = (
                "PAM_SUCCESS"
                if password_valid or "nullok" in line
                else "PAM_AUTH_ERR"
            )
        elif "pam_deny.so" in line:
            return False
        elif "pam_permit.so" in line:
            return True
        else:
            raise AssertionError("Unsupported PAM line: {}".format(line))

        controls = re.search(r"\[(.*?)\]", line)
        assert controls, "Missing PAM control expression: {}".format(line)
        actions = dict(
            token.split("=", 1)
            for token in controls.group(1).split()
        )
        action = actions.get(result_name[result], actions.get("default"))
        if action == "done":
            return True
        if action == "die":
            return False
        if action and action.isdigit():
            index += int(action) + 1
        else:
            assert action == "ignore"
            index += 1

    return False


@pytest.mark.parametrize("login", ["tacacs+", "tacacs+,local"])
@pytest.mark.parametrize("server_count", [0, 1, 3])
def test_tacacs_modes_share_local_fallback_without_nullok(login, server_count):
    lines = pam_auth_lines(
        render_authentication_config(login, server_count=server_count)
    )
    tacacs_indexes = [
        index for index, line in enumerate(lines) if "pam_tacplus.so" in line
    ]
    local_index = next(
        index for index, line in enumerate(lines) if "pam_unix.so" in line
    )

    assert len(tacacs_indexes) == server_count
    assert all(tacacs_index < local_index for tacacs_index in tacacs_indexes)
    assert "nullok" not in lines[local_index]


@pytest.mark.parametrize(
    "unavailable_result",
    ["PAM_SERVICE_ERR", "PAM_AUTHINFO_UNAVAIL"],
)
@pytest.mark.parametrize("failthrough", [False, True])
@pytest.mark.parametrize("login", ["tacacs+", "tacacs+,local"])
def test_unavailable_tacacs_falls_back_to_valid_local_password(
    login,
    failthrough,
    unavailable_result,
):
    lines = pam_auth_lines(
        render_authentication_config(
            login,
            failthrough=failthrough,
            server_count=2,
        )
    )

    assert authenticate(
        lines,
        tacacs_result=unavailable_result,
        password_valid=True,
    )


@pytest.mark.parametrize(
    "unavailable_result",
    ["PAM_SERVICE_ERR", "PAM_AUTHINFO_UNAVAIL"],
)
@pytest.mark.parametrize("failthrough", [False, True])
@pytest.mark.parametrize("login", ["tacacs+", "tacacs+,local"])
def test_unavailable_tacacs_rejects_empty_local_password(
    login,
    failthrough,
    unavailable_result,
):
    lines = pam_auth_lines(
        render_authentication_config(
            login,
            failthrough=failthrough,
            server_count=2,
        )
    )

    assert not authenticate(
        lines,
        tacacs_result=unavailable_result,
        password_valid=False,
    )


@pytest.mark.parametrize("failthrough", [False, True])
@pytest.mark.parametrize("login", ["tacacs+", "tacacs+,local"])
def test_tacacs_rejection_preserves_failthrough_behavior(
    login,
    failthrough,
):
    lines = pam_auth_lines(
        render_authentication_config(
            login,
            failthrough=failthrough,
            server_count=2,
        )
    )

    assert authenticate(
        lines,
        tacacs_result="PAM_AUTH_ERR",
        password_valid=True,
    ) is failthrough
    assert not authenticate(
        lines,
        tacacs_result="PAM_AUTH_ERR",
        password_valid=False,
    )


@pytest.mark.parametrize("login", ["tacacs+", "tacacs+,local"])
def test_tacacs_server_order_and_options_are_preserved(login):
    servers = [
        SimpleNamespace(
            ip="192.0.2.21",
            tcp_port=49,
            passkey="first-key",
            auth_type="pap",
            timeout=3,
            vrf="mgmt",
        ),
        SimpleNamespace(
            ip="192.0.2.22",
            tcp_port=1049,
            passkey="second-key",
            auth_type="chap",
            timeout=7,
            vrf=None,
        ),
        SimpleNamespace(
            ip="192.0.2.23",
            tcp_port=2049,
            passkey="third-key",
            auth_type="login",
            timeout=11,
            vrf="VrfTACACS",
        ),
    ]
    rendered = render_authentication_config(
        login,
        src_ip="198.51.100.10",
        servers=servers,
    )
    tacacs_lines = [
        line for line in pam_auth_lines(rendered) if "pam_tacplus.so" in line
    ]

    assert len(tacacs_lines) == len(servers)
    for line, server in zip(tacacs_lines, servers):
        assert "server={}:{}".format(server.ip, server.tcp_port) in line
        assert "secret={}".format(server.passkey) in line
        assert "login={}".format(server.auth_type) in line
        assert "timeout={}".format(server.timeout) in line
        assert ("vrf={}".format(server.vrf) in line) is bool(server.vrf)
        assert "source_ip=198.51.100.10" in line
