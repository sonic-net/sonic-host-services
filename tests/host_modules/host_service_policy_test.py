import ast
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "data" / "org.sonic.hostservice.conf"
INSTALL_PATH = REPO_ROOT / "data" / "debian" / "install"
SERVER_PATH = REPO_ROOT / "scripts" / "sonic-host-server"

BUS_NAME_BASE = "org.SONiC.HostService"
FILE_BUS_NAME = BUS_NAME_BASE + ".file"
FILE_BUS_PATH = "/org/SONiC/HostService/file"
FILE_MEMBER = "get_file_stat"
IMAGE_BUS_NAME = BUS_NAME_BASE + ".image_service"
IMAGE_BUS_PATH = "/org/SONiC/HostService/image_service"
IMAGE_MEMBER = "checksum"


def _registered_host_service_names():
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "mod_dict"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue

        names = {BUS_NAME_BASE}
        for handler in node.value.values:
            if not isinstance(handler, ast.Call) or not handler.args:
                raise AssertionError(
                    "HostService registration must have a literal module name"
                )
            try:
                module_name = ast.literal_eval(handler.args[0])
            except (ValueError, TypeError) as error:
                raise AssertionError(
                    "HostService registration must have a literal module name"
                ) from error
            if not isinstance(module_name, str):
                raise AssertionError(
                    "HostService registration must have a string module name"
                )
            if module_name != "host_service":
                names.add(BUS_NAME_BASE + "." + module_name)
        return names

    raise AssertionError("HostService module registration dictionary is missing")


# All modules use the shared SystemBus connection, so a destination grant to
# any claimed name can authorize calls to another object on the same owner.
HOST_SERVICE_BUS_NAMES = _registered_host_service_names()


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


def _allows_method_call(rule, interface, member, path):
    attributes = rule.attrib
    send_attributes = {
        name: value for name, value in attributes.items()
        if name.startswith("send_")
    }
    if not send_attributes:
        return not attributes

    if attributes.get("send_broadcast") == "true":
        return False

    return (
        _matches(attributes.get("send_type"), {"method_call"})
        and _matches(
            attributes.get("send_destination"),
            HOST_SERVICE_BUS_NAMES,
        )
        and _prefix_matches(
            attributes.get("send_destination_prefix"),
            HOST_SERVICE_BUS_NAMES,
        )
        and _matches(attributes.get("send_interface"), {interface})
        and _matches(attributes.get("send_member"), {member})
        and _matches(attributes.get("send_path"), {path})
        and _path_namespace_matches(
            attributes.get("send_path_namespace"),
            {path},
        )
    )


def _allows_file_stat(rule):
    return _allows_method_call(
        rule,
        FILE_BUS_NAME,
        FILE_MEMBER,
        FILE_BUS_PATH,
    )


def _allows_image_checksum(rule):
    return _allows_method_call(
        rule,
        IMAGE_BUS_NAME,
        IMAGE_MEMBER,
        IMAGE_BUS_PATH,
    )


def _allows_host_service_ownership(rule):
    own = rule.get("own")
    own_prefix = rule.get("own_prefix")
    return (
        own == "*" or own in HOST_SERVICE_BUS_NAMES
        or (
            own_prefix is not None
            and (
                own_prefix == "*"
                or any(
                    item == own_prefix or item.startswith(own_prefix + ".")
                    for item in HOST_SERVICE_BUS_NAMES
                )
            )
        )
    )


class TestHostServicePolicy:
    def test_registered_names_include_security_sensitive_endpoints(self):
        assert {
            BUS_NAME_BASE,
            BUS_NAME_BASE + ".config",
            FILE_BUS_NAME,
            IMAGE_BUS_NAME,
        }.issubset(HOST_SERVICE_BUS_NAMES)

    def test_file_stat_grant_detection_covers_broad_rules(self):
        matching_rules = [
            "<allow/>",
            '<allow send_destination="*"/>',
            '<allow send_destination="org.SONiC.HostService"/>',
            '<allow send_destination="org.SONiC.HostService.file"/>',
            '<allow send_destination="org.SONiC.HostService.config"/>',
            (
                '<allow send_destination="org.SONiC.HostService.image_service" '
                'send_interface="org.SONiC.HostService.file" '
                'send_member="get_file_stat" '
                'send_path="/org/SONiC/HostService/file" '
                'send_type="method_call"/>'
            ),
            '<allow send_destination_prefix="org.SONiC.HostService"/>',
            '<allow send_interface="org.SONiC.HostService.file"/>',
            '<allow send_member="get_file_stat"/>',
            '<allow send_path="/org/SONiC/HostService/file"/>',
            '<allow send_path_namespace="/org/SONiC/HostService"/>',
            '<allow send_requested_reply="true"/>',
            '<allow send_requested_reply="false"/>',
            '<allow send_error="org.example.Error"/>',
            '<allow send_type="method_call"/>',
        ]
        unrelated_rules = [
            '<allow receive_sender="org.SONiC.HostService"/>',
            '<allow send_type="signal"/>',
            '<allow send_broadcast="true"/>',
            '<allow send_interface="org.freedesktop.DBus.Introspectable"/>',
            '<allow send_interface="org.SONiC.HostService.image_service"/>',
            '<allow send_member="checksum"/>',
            '<allow send_path="/org/SONiC/HostService/image_service"/>',
        ]

        assert all(
            _allows_file_stat(ET.fromstring(rule))
            for rule in matching_rules
        )
        assert not any(
            _allows_file_stat(ET.fromstring(rule))
            for rule in unrelated_rules
        )

    def test_image_checksum_grant_detection_covers_co_owned_names(self):
        matching_rules = [
            "<allow/>",
            '<allow send_destination="*"/>',
            '<allow send_destination="org.SONiC.HostService"/>',
            '<allow send_destination="org.SONiC.HostService.image_service"/>',
            '<allow send_destination="org.SONiC.HostService.file"/>',
            '<allow send_destination="org.SONiC.HostService.config"/>',
            (
                '<allow send_destination="org.SONiC.HostService.file" '
                'send_interface="org.SONiC.HostService.image_service" '
                'send_member="checksum" '
                'send_path="/org/SONiC/HostService/image_service" '
                'send_type="method_call"/>'
            ),
            '<allow send_destination_prefix="org.SONiC.HostService"/>',
            '<allow send_interface="org.SONiC.HostService.image_service"/>',
            '<allow send_member="checksum"/>',
            '<allow send_path="/org/SONiC/HostService/image_service"/>',
            '<allow send_path_namespace="/org/SONiC/HostService"/>',
            '<allow send_requested_reply="true"/>',
            '<allow send_requested_reply="false"/>',
            '<allow send_error="org.example.Error"/>',
            '<allow send_type="method_call"/>',
        ]
        unrelated_rules = [
            '<allow receive_sender="org.SONiC.HostService"/>',
            '<allow send_type="signal"/>',
            '<allow send_broadcast="true"/>',
            '<allow send_interface="org.freedesktop.DBus.Introspectable"/>',
            '<allow send_interface="org.SONiC.HostService.file"/>',
            '<allow send_member="list_images"/>',
            '<allow send_path="/org/SONiC/HostService/file"/>',
        ]

        assert all(
            _allows_image_checksum(ET.fromstring(rule))
            for rule in matching_rules
        )
        assert not any(
            _allows_image_checksum(ET.fromstring(rule))
            for rule in unrelated_rules
        )

    def test_ownership_detection_covers_co_owned_names(self):
        matching_rules = [
            '<allow own="*"/>',
            '<allow own="org.SONiC.HostService"/>',
            '<allow own="org.SONiC.HostService.image_service"/>',
            '<allow own="org.SONiC.HostService.file"/>',
            '<allow own="org.SONiC.HostService.config"/>',
            '<allow own_prefix="org.SONiC.HostService"/>',
        ]
        unrelated_rules = [
            '<allow own="org.example.Service"/>',
            '<allow own_prefix="org.example"/>',
            '<allow send_destination="org.SONiC.HostService"/>',
        ]

        assert all(
            _allows_host_service_ownership(ET.fromstring(rule))
            for rule in matching_rules
        )
        assert not any(
            _allows_host_service_ownership(ET.fromstring(rule))
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

    def test_only_root_can_invoke_image_checksum(self):
        root = ET.parse(POLICY_PATH).getroot()
        matching_policies = [
            policy
            for policy in root.findall("policy")
            if any(
                _allows_image_checksum(rule)
                for rule in policy.findall("allow")
            )
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
