"""
    caclmgrd test redfish_acl vector

    Verifies that an ACL_TABLE with services=["REDFISH"] is translated into
    iptables rules targeting tcp/443. Mirrors test_external_client_acl_vectors
    so the assertion shape is consistent across services.
"""
REDFISH_ACL_TEST_VECTOR = [
    [
        "Test single IPv4 src ip allow + default deny for REDFISH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "REDFISH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "REDFISH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "REDFISH_ACL|ALLOW_MGMT": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "10.20.0.0/16"
                    },
                    "REDFISH_ACL|DENY_REST": {
                        "PACKET_ACTION": "DROP",
                        "PRIORITY": "1",
                        "SRC_IP": "0.0.0.0/0"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.20.0.0/16', '--dport', '443', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '0.0.0.0/0', '--dport', '443', '-j', 'DROP'],
            ],
        }
    ],
    [
        "Test IPv6 src ip allow for REDFISH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "REDFISH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "REDFISH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "REDFISH_ACL|ALLOW_MGMT_V6": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "2001:db8::/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '2001:db8::/32', '--dport', '443', '-j', 'ACCEPT'],
            ],
        }
    ],
]
