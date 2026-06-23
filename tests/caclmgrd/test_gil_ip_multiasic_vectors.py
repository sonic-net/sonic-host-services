from unittest.mock import call

"""
    caclmgrd test vectors for GIL IP multi-asic FORWARD chain rules.

    These tests verify that for non-default namespaces (e.g. asic0):
      1. FORWARD ACCEPT rules are inserted for SSH/SNMP (multi_asic_ns_to_host_fwd=True)
         sourced from the namespace management IPs (GIL IPs).
      2. ACL rules for SSH/SNMP use the FORWARD chain instead of INPUT.
      3. FORWARD DROP rules are appended after INPUT DROP when ACL rules exist.
      4. Default namespace (empty string) still uses INPUT chain (no regression).
"""

NAMESPACE_MGMT_IP = "10.1.0.1"
NAMESPACE_MGMT_IPV6 = "fc00::1"
ASIC0_NS_PREFIX = ['ip', 'netns', 'exec', 'asic0']

# ---------------------------------------------------------------------------
# Helper: wrap a rule list with the asic0 namespace prefix
# ---------------------------------------------------------------------------
def _ns(rule):
    return ASIC0_NS_PREFIX + rule


CACLMGRD_GIL_IP_MULTIASIC_TEST_VECTOR = [
    # ------------------------------------------------------------------
    # 1. Non-default namespace: GIL FORWARD ACCEPT rules generated for
    #    SSH and SNMP even when no ACL tables are configured.
    # ------------------------------------------------------------------
    [
        "Non-default namespace: GIL FORWARD ACCEPT rules added for SSH and SNMP",
        {
            "namespace": "asic0",
            "config_db": {
                "ACL_TABLE": {},
                "ACL_RULE": {},
                "DEVICE_METADATA": {"localhost": {}},
                "FEATURE": {},
            },
            # Rules that MUST be present in the output
            "expected_present": [
                _ns(['ip6tables', '-A', 'FORWARD', '-p', 'tcp', '-s', NAMESPACE_MGMT_IPV6, '--sport', '22', '-j', 'ACCEPT']),
                _ns(['iptables',  '-A', 'FORWARD', '-p', 'tcp', '-s', NAMESPACE_MGMT_IP,   '--sport', '22', '-j', 'ACCEPT']),
                _ns(['ip6tables', '-A', 'FORWARD', '-p', 'tcp', '-s', NAMESPACE_MGMT_IPV6, '--sport', '161', '-j', 'ACCEPT']),
                _ns(['iptables',  '-A', 'FORWARD', '-p', 'tcp', '-s', NAMESPACE_MGMT_IP,   '--sport', '161', '-j', 'ACCEPT']),
                _ns(['ip6tables', '-A', 'FORWARD', '-p', 'udp', '-s', NAMESPACE_MGMT_IPV6, '--sport', '161', '-j', 'ACCEPT']),
                _ns(['iptables',  '-A', 'FORWARD', '-p', 'udp', '-s', NAMESPACE_MGMT_IP,   '--sport', '161', '-j', 'ACCEPT']),
            ],
            # Rules that must NOT be present (NTP has multi_asic_ns_to_host_fwd=False)
            "expected_absent": [
                _ns(['iptables',  '-A', 'FORWARD', '-p', 'udp', '-s', NAMESPACE_MGMT_IP,   '--sport', '123', '-j', 'ACCEPT']),
                _ns(['ip6tables', '-A', 'FORWARD', '-p', 'udp', '-s', NAMESPACE_MGMT_IPV6, '--sport', '123', '-j', 'ACCEPT']),
            ],
        }
    ],

    # ------------------------------------------------------------------
    # 2. Non-default namespace: ACL rules for SSH use FORWARD chain.
    # ------------------------------------------------------------------
    [
        "Non-default namespace: SSH ACL rule uses FORWARD chain",
        {
            "namespace": "asic0",
            "config_db": {
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": ["SSH"],
                    }
                },
                "ACL_RULE": {
                    "SSH_ONLY|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "192.168.1.0/24",
                    },
                },
                "DEVICE_METADATA": {"localhost": {}},
                "FEATURE": {},
            },
            "expected_present": [
                _ns(['iptables', '-A', 'FORWARD', '-p', 'tcp', '-s', '192.168.1.0/24', '--dport', '22', '-j', 'ACCEPT']),
                # FORWARD DROP added because num_ctrl_plane_acl_rules > 0
                _ns(['iptables',  '-A', 'FORWARD', '-j', 'DROP']),
                _ns(['ip6tables', '-A', 'FORWARD', '-j', 'DROP']),
            ],
            "expected_absent": [
                # Must NOT use INPUT for SSH in non-default namespace
                _ns(['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '192.168.1.0/24', '--dport', '22', '-j', 'ACCEPT']),
            ],
        }
    ],

    # ------------------------------------------------------------------
    # 3. Non-default namespace: SNMP ACL rule uses FORWARD chain.
    # ------------------------------------------------------------------
    [
        "Non-default namespace: SNMP ACL rule uses FORWARD chain",
        {
            "namespace": "asic0",
            "config_db": {
                "ACL_TABLE": {
                    "SNMP_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": ["SNMP"],
                    }
                },
                "ACL_RULE": {
                    "SNMP_ACL|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "10.0.0.0/8",
                    },
                },
                "DEVICE_METADATA": {"localhost": {}},
                "FEATURE": {},
            },
            "expected_present": [
                _ns(['iptables', '-A', 'FORWARD', '-p', 'tcp', '-s', '10.0.0.0/8', '--dport', '161', '-j', 'ACCEPT']),
                _ns(['iptables', '-A', 'FORWARD', '-p', 'udp', '-s', '10.0.0.0/8', '--dport', '161', '-j', 'ACCEPT']),
                _ns(['iptables',  '-A', 'FORWARD', '-j', 'DROP']),
                _ns(['ip6tables', '-A', 'FORWARD', '-j', 'DROP']),
            ],
            "expected_absent": [
                _ns(['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.0/8', '--dport', '161', '-j', 'ACCEPT']),
            ],
        }
    ],

    # ------------------------------------------------------------------
    # 4. Non-default namespace: NTP ACL rule still uses INPUT chain
    #    (multi_asic_ns_to_host_fwd=False for NTP).
    # ------------------------------------------------------------------
    [
        "Non-default namespace: NTP ACL rule still uses INPUT chain",
        {
            "namespace": "asic0",
            "config_db": {
                "ACL_TABLE": {
                    "NTP_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": ["NTP"],
                    }
                },
                "ACL_RULE": {
                    "NTP_ACL|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "10.0.0.1/32",
                    },
                },
                "DEVICE_METADATA": {"localhost": {}},
                "FEATURE": {},
            },
            "expected_present": [
                _ns(['iptables', '-A', 'INPUT', '-p', 'udp', '-s', '10.0.0.1/32', '--dport', '123', '-j', 'ACCEPT']),
            ],
            "expected_absent": [
                _ns(['iptables', '-A', 'FORWARD', '-p', 'udp', '-s', '10.0.0.1/32', '--dport', '123', '-j', 'ACCEPT']),
            ],
        }
    ],

    # ------------------------------------------------------------------
    # 5. Default namespace: SSH ACL still uses INPUT chain (no regression).
    # ------------------------------------------------------------------
    [
        "Default namespace: SSH ACL rule uses INPUT chain (no regression)",
        {
            "namespace": "",
            "config_db": {
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": ["SSH"],
                    }
                },
                "ACL_RULE": {
                    "SSH_ONLY|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "192.168.1.0/24",
                    },
                },
                "DEVICE_METADATA": {"localhost": {}},
                "FEATURE": {},
            },
            "expected_present": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '192.168.1.0/24', '--dport', '22', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-j', 'DROP'],
                ['ip6tables', '-A', 'INPUT', '-j', 'DROP'],
            ],
            "expected_absent": [
                # No FORWARD DROP for default namespace
                ['iptables',  '-A', 'FORWARD', '-j', 'DROP'],
                ['ip6tables', '-A', 'FORWARD', '-j', 'DROP'],
                # GIL FORWARD ACCEPT rules must not appear for default namespace
                ['iptables',  '-A', 'FORWARD', '-p', 'tcp', '-s', NAMESPACE_MGMT_IP, '--sport', '22', '-j', 'ACCEPT'],
            ],
        }
    ],
]
