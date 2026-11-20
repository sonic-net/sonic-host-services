"""
    caclmgrd control plane ACL rule priority test vector

    Each "return" lists the iptables commands generated from ACL_RULE
    entries in the order caclmgrd must issue them: highest PRIORITY first.
"""
CACLMGRD_RULE_PRIORITY_TEST_VECTOR = [
    [
        "Priority 10000 DROP is installed before priority 9999 ACCEPT",
        {
            "config_db": {
                "ACL_TABLE": {
                    "SNMP_ACL": {
                        "stage": "ingress",
                        "type": "CTRLPLANE",
                        "services": [
                            "SNMP"
                        ]
                    }
                },
                "ACL_RULE": {
                    "SNMP_ACL|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "20.20.20.2/32"
                    },
                    "SNMP_ACL|DENY_RULE": {
                        "PACKET_ACTION": "DROP",
                        "PRIORITY": "10000",
                        "SRC_IP": "20.20.20.2/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-p', 'udp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'udp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "Priorities with the same number of digits keep their order",
        {
            "config_db": {
                "ACL_TABLE": {
                    "SNMP_ACL": {
                        "stage": "ingress",
                        "type": "CTRLPLANE",
                        "services": [
                            "SNMP"
                        ]
                    }
                },
                "ACL_RULE": {
                    "SNMP_ACL|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9000",
                        "SRC_IP": "20.20.20.2/32"
                    },
                    "SNMP_ACL|DENY_RULE": {
                        "PACKET_ACTION": "DROP",
                        "PRIORITY": "9999",
                        "SRC_IP": "20.20.20.2/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-p', 'udp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'udp', '-s', '20.20.20.2/32', '--dport', '161', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "Priorities with one, two and three digits are installed in numeric order",
        {
            "config_db": {
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "stage": "ingress",
                        "type": "CTRLPLANE",
                        "services": [
                            "SSH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "SSH_ONLY|RULE_2": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "2",
                        "SRC_IP": "10.0.0.2/32"
                    },
                    "SSH_ONLY|RULE_10": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "10",
                        "SRC_IP": "10.0.0.10/32"
                    },
                    "SSH_ONLY|RULE_100": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "100",
                        "SRC_IP": "10.0.0.100/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.100/32', '--dport', '22', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.10/32', '--dport', '22', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.2/32', '--dport', '22', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "IPv6 rule in an IPv4 table is ignored and the IPv4 rules stay in numeric order",
        {
            "config_db": {
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "stage": "ingress",
                        "type": "CTRLPLANE",
                        "services": [
                            "SSH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "SSH_ONLY|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9999",
                        "SRC_IP": "10.0.0.1/32"
                    },
                    "SSH_ONLY|RULE_2": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "100000",
                        "SRC_IPV6": "2001::1/128"
                    },
                    "SSH_ONLY|DENY_RULE": {
                        "PACKET_ACTION": "DROP",
                        "PRIORITY": "10000",
                        "SRC_IP": "10.0.0.1/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.1/32', '--dport', '22', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '10.0.0.1/32', '--dport', '22', '-j', 'ACCEPT'],
            ],
        }
    ],
]
