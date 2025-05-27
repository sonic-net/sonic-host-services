from unittest.mock import call
import subprocess

"""
    caclmgrd soc test vector
"""
BGP_LOOPBACK1_TEST_VECTOR = [
    [
        "BGP_LOOPBACK1_SESSION_TEST",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "MUX_CABLE": {
                    "Ethernet4": {
                        "cable_type": "active-active",
                        "soc_ipv4": "10.10.10.7/32",
                    }
                },
                "VLAN_INTERFACE": {
                    "Vlan1000|10.10.10.3/24": {
                        "NULL": "NULL",
                    }
                },
                "LOOPBACK_INTERFACE": {
                    "Loopback1|10.10.10.34/32": {
                        "NULL": "NULL",
                    }
                },
                "FEATURE": {
                },
            },
            "expected_subprocess_calls": [
                call(['iptables', '-A', 'INPUT', '-d', "10.10.10.34", '-p', 'tcp', '--dport', '179', '-j', 'DROP'], universal_newlines=True, stdout=-1)
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error'),
            },
            "call_rc": 0,
        }
    ]
]