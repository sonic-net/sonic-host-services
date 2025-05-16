from unittest.mock import call
import subprocess

"""
    caclmgrd dash-ha test vector
"""
CACLMGRD_DASH_HA_TEST_VECTOR = [
    [
        "DASH_HA_TEST",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "SmartSwitch",
                        "type": "LeafRouter",
                    }
                },                
                "FEATURE": {
                    "dash-ha": {
                        "auto_restart": "enabled",
                        "has_per_dpu_scope": "True",
                        "state": "enabled",
                    }
                }            
            },
            "input_add" : [("swbus_port", "23606")],
            "input_upd" : [("swbus_port", "23607")],
            "expected_add_subprocess_calls": [
                call(['iptables', '-I', 'INPUT', '2', '-p', 'tcp', '--dport', '23606', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-I', 'INPUT', '2', '-p', 'tcp', '--dport', '23606', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
            ],
            "expected_upd_subprocess_calls": [
                call(['iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', '23606', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-D', 'INPUT', '-p', 'tcp', '--dport', '23606', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['iptables', '-I', 'INPUT', '2', '-p', 'tcp', '--dport', '23607', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-I', 'INPUT', '2', '-p', 'tcp', '--dport', '23607', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
            ],
            "expected_del_subprocess_calls": [
                call(['iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', '23607', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
                call(['ip6tables', '-D', 'INPUT', '-p', 'tcp', '--dport', '23607', '-j', 'ACCEPT'], universal_newlines=True, stdout=subprocess.PIPE),
            ],
            
            "popen_attributes": {
                'communicate.return_value': ('output', 'error'),
            },
            "call_rc": 0,
        }
    ]
]
