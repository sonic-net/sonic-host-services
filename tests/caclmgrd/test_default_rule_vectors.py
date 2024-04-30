from unittest.mock import call
import subprocess

"""
caclmgrd default rule test vector
"""
CACLMGRD_DEFAULT_RULE_TEST_VECTOR = [
    (
        "No CACL rules -> Default deny rule NOT EXISTS",
        {
            "config_db": {
                "ACL_RULE": {},
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "policy_desc": "SSH_ONLY",
                        "services": [
                            "SSH"
                        ],
                        "stage": "ingress",
                        "type": "CTRLPLANE"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [],
            "default_deny": False,
        }
    ),
    (
        "At least one CACL rules -> Default deny rule EXISTS",
        {
            "config_db": {
                "ACL_RULE": {
                    "SSH_ONLY|RULE_22": {
                        "PACKET_ACTION": "ACCEPT",
                        "PRIORITY": "9978",
                        "SRC_IPV6": "2001::23/128"
                    },
                },
                "ACL_TABLE": {
                    "SSH_ONLY": {
                        "policy_desc": "SSH_ONLY",
                        "services": [
                            "SSH"
                        ],
                        "stage": "ingress",
                        "type": "CTRLPLANE"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['ip6tables', '-A', 'INPUT', '-p', 'tcp', '-s', '2001::23/128', '--dport', '22', '-j', 'ACCEPT'],
                ['iptables', '-A', 'INPUT', '-j', 'DROP'],
                ['ip6tables', '-A', 'INPUT', '-j', 'DROP'],
            ],
            "default_deny": True,
        }
    ),
]
