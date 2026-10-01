from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "data" / "org.sonic.hostservice.conf"
INSTALL_PATH = REPO_ROOT / "data" / "debian" / "install"

BUS_NAME_BASE = "org.SONiC.HostService"
FILE_BUS_NAME = BUS_NAME_BASE + ".file"
FILE_BUS_PATH = "/org/SONiC/HostService/file"
FILE_MEMBER = "get_file_stat"


def _is_root_policy(policy):
    return policy.attrib == {"user": "root"}


def _matches(value, expected):
    return value is None or value == "*" or value in expected


def _prefix_matches(value, expected):
    if value is None:
        return True
    if value == "*":
        return True
    return any(item == value or item.startswith(value + ".") for item in expected)


def _path_namespace_matches(value, expected):
    if value is None:
        return True
    if value == "*":
        return True
    value = value.rstrip("/")
    return any(item == value or item.startswith(value + "/") for item in expected)


def _allows_file_stat(rule):
    attributes = rule.attrib
    send_attributes = {
        name: value for name, value in attributes.items()
        if name.startswith("send_")
    }
    if not send_attributes:
        return not attributes

    if (
        attributes.get("send_requested_reply") == "true"
        or "send_error" in attributes
    ):
        return False

    return (
        _matches(attributes.get("send_type"), {"method_call"})
        and _matches(
            attributes.get("send_destination"),
            {BUS_NAME_BASE, FILE_BUS_NAME},
        )
        and _prefix_matches(
            attributes.get("send_destination_prefix"),
            {BUS_NAME_BASE, FILE_BUS_NAME},
        )
        and _matches(attributes.get("send_interface"), {FILE_BUS_NAME})
        and _matches(attributes.get("send_member"), {FILE_MEMBER})
        and _matches(attributes.get("send_path"), {FILE_BUS_PATH})
        and _path_namespace_matches(
            attributes.get("send_path_namespace"),
            {FILE_BUS_PATH},
        )
    )


def _allows_host_service_ownership(rule):
    own = rule.get("own")
    own_prefix = rule.get("own_prefix")
    return (
        own in {"*", BUS_NAME_BASE, FILE_BUS_NAME}
        or (
            own_prefix is not None
            and (
                own_prefix == "*"
                or any(
                    item == own_prefix or item.startswith(own_prefix + ".")
                    for item in {BUS_NAME_BASE, FILE_BUS_NAME}
                )
            )
        )
    )


class TestHostServicePolicy:
    def test_file_stat_grant_detection_covers_broad_rules(self):
        matching_rules = [
            "<allow/>",
            '<allow send_destination="org.SONiC.HostService"/>',
            '<allow send_destination="org.SONiC.HostService.file"/>',
            '<allow send_destination_prefix="org.SONiC.HostService"/>',
            '<allow send_interface="org.SONiC.HostService.file"/>',
            '<allow send_member="get_file_stat"/>',
            '<allow send_path="/org/SONiC/HostService/file"/>',
            '<allow send_type="method_call"/>',
        ]
        unrelated_rules = [
            '<allow receive_sender="org.SONiC.HostService"/>',
            '<allow send_type="signal"/>',
            '<allow send_interface="org.freedesktop.DBus.Introspectable"/>',
            '<allow send_requested_reply="true"/>',
            '<allow send_error="org.example.Error"/>',
        ]

        assert all(
            _allows_file_stat(ET.fromstring(rule))
            for rule in matching_rules
        )
        assert not any(
            _allows_file_stat(ET.fromstring(rule))
            for rule in unrelated_rules
        )

    def test_only_root_can_own_host_service_names(self):
        root = ET.parse(POLICY_PATH).getroot()
        matching_policies = [
            policy
            for policy in root.findall("policy")
            if any(
                _allows_host_service_ownership(rule)
                for rule in policy.findall("allow")
            )
        ]

        assert matching_policies, "HostService ownership grant is missing"
        assert all(_is_root_policy(policy) for policy in matching_policies)

    def test_only_root_can_invoke_file_stat(self):
        root = ET.parse(POLICY_PATH).getroot()
        matching_policies = [
            policy
            for policy in root.findall("policy")
            if any(_allows_file_stat(rule) for rule in policy.findall("allow"))
        ]

        assert matching_policies, "HostService method-call grant is missing"
        assert all(_is_root_policy(policy) for policy in matching_policies)

    def test_policy_is_installed_for_the_system_bus(self):
        install_entries = [
            line.split()
            for line in INSTALL_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        assert [
            POLICY_PATH.name,
            "/etc/dbus-1/system.d",
        ] in install_entries
