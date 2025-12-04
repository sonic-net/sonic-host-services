"""
    hostcfgd test password hardening vector
"""
HOSTCFGD_TEST_SSH_SERVER_VECTOR = [
    [
        "SSH_SERVER",
        {
            "default_values":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "120",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0"
                    }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-16G:448M,16G-32G:768M,32G-:1G"
                    }
                },
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_authentication_retries":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "12",
                        "login_timeout": "120",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                    }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-16G:448M,16G-32G:768M,32G-:1G"
                    }
                },
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_login_timeout":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                    }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-16G:448M,16G-32G:768M,32G-:1G"
                    }
                },
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_ports":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "120",
                        "ports": "22,23,24",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                    }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-16G:448M,16G-32G:768M,32G-:1G"
                    }
                },
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
           "modify_password_authentication":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "password_authentication": "false"
                    }
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
                        "has_timer": "False",
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
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
           "modify_permit_root_login":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "permit_root_login": "no"
                    }
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
                        "has_timer": "False",
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
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
           "modify_ciphers":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "ciphers": [ "chacha20-poly1305@openssh.com", "aes256-gcm@openssh.com" ]
                    }
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
                        "has_timer": "False",
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
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_kex_algorithms":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "kex_algorithms": [ "sntrup761x25519-sha512", "curve25519-sha256", "ecdh-sha2-nistp521" ]
                    }
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
                        "has_timer": "False",
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
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_macs":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "6",
                        "login_timeout": "60",
                        "ports": "22",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "macs": [ "hmac-sha2-512-etm@openssh.com", "hmac-sha2-512" ]
                    }
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
                        "has_timer": "False",
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
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            },
            "modify_all":{
                "SSH_SERVER": {
                    "POLICIES":{
                        "authentication_retries": "16",
                        "login_timeout": "140",
                        "ports": "22,222",
                        "inactivity_timeout": "15",
                        "max_sessions": "0",
                        "permit_root_login": "no",
                        "password_authentication": "false",
                        "ciphers": [ "chacha20-poly1305@openssh.com", "aes256-gcm@openssh.com" ],
                        "kex_algorithms": [ "sntrup761x25519-sha512", "curve25519-sha256", "ecdh-sha2-nistp521" ],
                        "macs": [ "hmac-sha2-512-etm@openssh.com", "hmac-sha2-512" ]
                    }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-16G:448M,16G-32G:768M,32G-:1G"
                    }
                },
                "SERIAL_CONSOLE": {
                    "POLICIES":{
                        "inactivity_timeout": "15",
                        "sysrq_capabilities": "disabled"
                    }
                }
            }
        }
    ]
]
