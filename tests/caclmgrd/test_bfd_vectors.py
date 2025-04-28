from unittest.mock import call
import subprocess

"""
    caclmgrd bfd test vector
"""
CACLMGRD_BFD_TEST_VECTOR = [
    [
        "BFD_SESSION_TEST",
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
                "LOOPBACK_INTERFACE": {
                    "Loopback0|2.2.2.1/32": {},
                    "Loopback0|2001:db8:10::/64": {}
                },
            },
            "expected_subprocess_calls": [
                call(['iptables', '-I', 'INPUT', '2', '-p', 'udp', '-m', 'multiport', '--dports', '3784,4784', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-I', 'INPUT', '2', '-p', 'udp', '-m', 'multiport', '--dports', '3784,4784', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['iptables', '-A', 'INPUT', '-d', '2.2.2.1/32', '-j', 'DROP'],universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-A', 'INPUT', '-d', '2001:db8:10::/128', '-j', 'DROP'],universal_newlines=True, stdout=subprocess.PIPE)
            ],
            "expected_bfd_subprocess_calls": [
                call(['iptables', '-I', 'INPUT', '2', '-p', 'udp', '-m', 'multiport', '--dports', '3784,4784', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-I', 'INPUT', '2', '-p', 'udp', '-m', 'multiport', '--dports', '3784,4784', '-j', 'ACCEPT', '!', '-i', 'eth0'], universal_newlines=True, stdout=subprocess.PIPE)
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error'),
            },
            "call_rc": 0,
        }
    ]
]
