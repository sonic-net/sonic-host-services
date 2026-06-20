from unittest.mock import call

"""
    caclmgrd test match vector
"""
MATCH_ACL_TEST_VECTOR = [
    [
        "Test for MATCH_ACL with no dest port configured.",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "20.0.0.55/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.0.0.55/32', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "Test single IPv4 dst port + src ip for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "L4_DST_PORT": "8081",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "20.0.0.55/32"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.0.0.55/32', '--dport', '8081', '-j', 'ACCEPT']
            ],
        }
    ],
    [
        "Test multiple IPv4 dst port + src ip for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "L4_DST_PORT": "67",
                        "SRC_IP": "0.0.0.0/0",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998"
                    },
                    "MATCH_ACL|RULE_2": {
                        "L4_DST_PORT": "68",
                        "SRC_IP": "0.0.0.0/0",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997"
                    },
                    "MATCH_ACL|RULE_3": {
                        "IP_PROTOCOL": "17",
                        "L4_DST_PORT": "67",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9996",
                        "SRC_IP": "0.0.0.0/0"
                    },
                    "MATCH_ACL|RULE_4": {
                        "IP_PROTOCOL": "17",
                        "L4_DST_PORT": "68",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9995",
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
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '0.0.0.0/0', '--dport', '67', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '0.0.0.0/0', '--dport', '68', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '17', '-s', '0.0.0.0/0', '--dport', '67', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '17', '-s', '0.0.0.0/0', '--dport', '68', '-j', 'ACCEPT']
            ],
        }
    ],
    [
        "Test IPv4 dst port range + src ip for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "L4_DST_PORT_RANGE": "8081-8083",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "20.0.0.55/32"
                    },
                    "MATCH_ACL|RULE_2": {
                        "IP_PROTOCOL": "17",
                        "L4_DST_PORT_RANGE": "50000-50100",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997",
                        "SRC_IP": "0.0.0.0/0"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', 'tcp', '-s', '20.0.0.55/32', '--dport', '8081:8083', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '17', '-s', '0.0.0.0/0', '--dport', '50000:50100', '-j', 'ACCEPT']
            ],
        }
    ],
    [
        "Test IPv4 protocol type of vrrp, ospf, igmp, pim for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "IP_PROTOCOL": "2",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IP": "0.0.0.0/0"
                    },
                    "MATCH_ACL|RULE_2": {
                        "IP_PROTOCOL": "89",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997",
                        "SRC_IP": "0.0.0.0/0"
                    },
                    "MATCH_ACL|RULE_3": {
                        "IP_PROTOCOL": "103",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9996",
                        "SRC_IP": "0.0.0.0/0"
                    },
                    "MATCH_ACL|RULE_4": {
                        "IP_PROTOCOL": "112",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9995",
                        "SRC_IP": "0.0.0.0/0"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-p', '2', '-s', '0.0.0.0/0', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '89', '-s', '0.0.0.0/0', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '103', '-s', '0.0.0.0/0', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-p', '112', '-s', '0.0.0.0/0', '-j', 'ACCEPT']
            ],
        }
    ],
    [
        "Test IPv6 single dst port range + src ip for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "L4_DST_PORT": "8081",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IPV6": "2001::2/128"
                    },
                    "MATCH_ACL|RULE_2": {
                        "IP_PROTOCOL": "17",
                        "L4_DST_PORT": "67",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997",
                        "SRC_IPV6": "2001::2/128"
                    },
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['ip6tables', '-A', 'INPUT', '-p', 'tcp', '-s', '2001::2/128', '--dport', '8081', '-j', 'ACCEPT'],
                ['ip6tables', '-A', 'INPUT', '-p', '17', '-s', '2001::2/128', '--dport', '67', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "Test IPv6 dst port range + src ip for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "L4_DST_PORT_RANGE": "8081-8083",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IPV6": "2001::2/128"
                    },
                    "MATCH_ACL|RULE_2": {
                        "IP_PROTOCOL": "17",
                        "L4_DST_PORT_RANGE": "50000-50100",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997",
                        "SRC_IPV6": "2001::2/128"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['ip6tables', '-A', 'INPUT', '-p', 'tcp', '-s', '2001::2/128', '--dport', '8081:8083', '-j', 'ACCEPT'],
                ['ip6tables', '-A', 'INPUT', '-p', '17', '-s', '2001::2/128', '--dport', '50000:50100', '-j', 'ACCEPT'],
            ],
        }
    ],
    [
        "Test IPv6 protocol type of vrrp, ospf for MATCH_ACL",
        {
            "config_db": {
                "ACL_TABLE": {
                    "MATCH_ACL": {
                        "stage": "INGRESS",
                        "type": "CTRLPLANE",
                        "services": [
                            "MATCH"
                        ]
                    }
                },
                "ACL_RULE": {
                    "MATCH_ACL|RULE_1": {
                        "IP_PROTOCOL": "89",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9998",
                        "SRC_IPV6": "::/0"
                    },
                    "MATCH_ACL|RULE_2": {
                        "IP_PROTOCOL": "112",
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9997",
                        "SRC_IPV6": "::/0"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['ip6tables', '-A', 'INPUT', '-p', '89', '-s', '::/0', '-j', 'ACCEPT'],
                ['ip6tables', '-A', 'INPUT', '-p', '112', '-s', '::/0', '-j', 'ACCEPT']
            ],
        }
    ]
]

