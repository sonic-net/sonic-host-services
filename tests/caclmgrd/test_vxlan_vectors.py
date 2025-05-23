from unittest.mock import call
import subprocess

"""
    caclmgrd bfd test vector
"""
CACLMGRD_VXLAN_TEST_VECTOR = [
    [
        "VXLAN_TUNNEL_TEST_V4",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "state": "enabled",
                    }
                },
            },
            "input" : [("src_ip", "10.1.1.1")],
            "expected_add_subprocess_calls": [
                call(['iptables', '-I', 'INPUT', '2', '-p', 'udp', '-d', '10.1.1.1', '--dport', '4789', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE)],
            "expected_del_subprocess_calls": [
                call(['iptables', '-D', 'INPUT', '-p', 'udp', '-d', '10.1.1.1', '--dport', '4789', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE)
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error'),
            },
            "call_rc": 0,
        }
        ],
        [
        "VXLAN_TUNNEL_TEST_V6",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "state": "enabled",
                    }
                },
            },
            "input" : [("src_ip", "2001::1")],
            "expected_add_subprocess_calls": [
                call(['ip6tables', '-I', 'INPUT', '2', '-p', 'udp', '-d', '2001::1', '--dport', '4789', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE)],
            "expected_del_subprocess_calls": [
                call(['ip6tables', '-D', 'INPUT', '-p', 'udp', '-d', '2001::1', '--dport', '4789', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE)
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error'),
            },
            "call_rc": 0,
        }
    ]
]
