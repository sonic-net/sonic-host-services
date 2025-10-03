from unittest.mock import call

"""
    hostcfgd test radius vector
"""
HOSTCFGD_TEST_RADIUS_VECTOR = [
    [
        "RADIUS",
        {
            "config_db": {
               "MGMT_INTERFACE": {
                    "eth0|1.1.1.15/23": {
                        "gwaddr": "1.1.1.10"
                    },
                    "eth0|2404::2/64": {
                        "gwaddr": "2404::1"
                    }
                },
                "PORTCHANNEL_INTERFACE": {
                    "PortChannel0001|10.10.11.10/32": {}
                 },
                "DEVICE_METADATA": {
                    "localhost": {
                        "hostname": "radius",
                    }
                },
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                        }
                },
                "AAA": {
                    "authentication": {
                        "login": "radius,local",
                        "debug": "True",
                    }
                },
                "RADIUS": {
                    "global": {
                        "nas_ip": "10.10.10.10",
                        "auth_port": "1645",
                        "auth_type": "mschapv2",
                        "retransmit": "2",
                        "timeout": "3",
                        "passkey": "pass",
                    }
                },
                "RADIUS_SERVER": {
                    "10.10.10.1": {
                        "auth_type": "pap",
                        "retransmit": "1",
                        "timeout": "1",
                        "passkey": "pass1",
                    },
                    "10.10.10.2": {
                        "auth_type": "chap",
                        "retransmit": "2",
                        "timeout": "2",
                        "passkey": "pass2",
                    },
                    "10.10.10.3": {
                        "auth_type": "chap",
                        "retransmit": "3",
                        "timeout": "3",
                        "passkey": "pass3",
                        "src_intf": "PortChannel0001",
                    },
                    "10.10.10.4": {
                        "auth_type": "pap",
                        "retransmit": "4",
                        "timeout": "4",
                        "passkey": "pass4",
                        "src_intf": "eth0",
                    },
                    "10.10.10.5": {
                        "auth_type": "pap",
                        "retransmit": "1",
                        "timeout": "1",
                        "passkey": "pass1",
                        "skip_msg_auth": "true",
                    }
                },
            },
            "expected_config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "hostname": "radius",
                    }
                },
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "AAA": {
                    "authentication": {
                        "login": "radius,local",
                        "debug": "True",
                    }
                },
                "RADIUS": {
                    "global": {
                        "nas_ip": "10.10.10.10",
                        "auth_port": "1645",
                        "auth_type": "mschapv2",
                        "retransmit": "2",
                        "timeout": "3",
                        "passkey": "pass",
                    }
                },
                "RADIUS_SERVER": {
                    "10.10.10.1": {
                        "auth_type": "pap",
                        "retransmit": "1",
                        "timeout": "1",
                        "passkey": "pass1",
                    },
                    "10.10.10.2": {
                        "auth_type": "chap",
                        "retransmit": "2",
                        "timeout": "2",
                        "passkey": "pass2",
                    },
                    "10.10.10.3": {
                        "auth_type": "chap",
                        "retransmit": "3",
                        "timeout": "3",
                        "passkey": "pass3",
                        "src_intf": "PortChannel0001",
                    },
                    "10.10.10.4": {
                        "auth_type": "pap",
                        "retransmit": "4",
                        "timeout": "4",
                        "passkey": "pass4",
                        "src_intf": "eth0",
                    },
                    "10.10.10.5": {
                        "auth_type": "pap",
                        "retransmit": "1",
                        "timeout": "1",
                        "passkey": "pass1",
                        "skip_msg_auth": "true",
                    }
                },
            },
            "expected_subprocess_calls": [
                call(["service", "aaastatsd", "start"]),
            ],
        }
    ],
    [
        "LOCAL",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "hostname": "local",
                    }
                },
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                        }
                },
                "AAA": {
                    "authentication": {
                        "login": "local",
                        "debug": "True",
                    }
                },
            },
            "expected_config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "hostname": "local",
                    }
                },
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "AAA": {
                    "authentication": {
                        "login": "local",
                        "debug": "True",
                    }
                },
            },
            "expected_subprocess_calls": [
                call(["service", "aaastatsd", "start"]),
            ],
        },
    ],
]
