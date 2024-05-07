from unittest.mock import call

"""
    hostcfgd test ldap vector
"""
HOSTCFGD_TEST_LDAP_VECTOR = [
    [
        "LDAP",
        {
            "config_db": {
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
                "AAA": {
                    "authentication": {
                        "login": "ldap,local",
                        "restrictions":{
                            "lockout-state": "enabled",
                            "fail-delay": 0,
                            "lockout-reattempt": 15,
                            "lockout-attempts": 5
                        },                        
                        "failthrough": "True",
                        "debug": "True",
                    }
                },
                "LDAP": {
                    "global": {
                        "port": "389",
                        "bind_password": "pass",
                        "bind_dn": "cn=ldapadm,dc=example,dc=com",
                        "base_dn": "ou=users,dc=example,dc=com",
                        "bind_timeout": "2",
                        "search_timeout": "2",
                        "version": "3"
                    }
                },
                "LDAP_SERVER": {
                    "10.10.10.1": {
                        "priority": "1"
                    },
                    "10.10.10.2": {
                        "priority": "2"
                    }
                },
                "SSH_SERVER": {
                    "POLICIES" :{
                        "max_sessions": "100"
                    }
                }
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
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled"
                    },
                },
                "AAA": {
                    "authentication": {
                        "login": "ldap,local",
                        "restrictions":{
                            "lockout-state": "enabled",
                            "fail-delay": 0,
                            "lockout-reattempt": 15,
                            "lockout-attempts": 5
                        },   
                        "debug": "True",
                    }
                },
                "LDAP": {
                    "global": {
                        "auth_port": "389",
                        "timeout": "3",
                        "passkey": "pass",
                    }
                },
                "LDAP_SERVER": {
                    "10.10.10.1": {
                        "priority": "1",
                        "passkey": "pass1",
                    },
                    "10.10.10.2": {
                        "priority": "2",
                    }
                },
                "SSH_SERVER": {
                    "POLICIES" :{
                        "max_sessions": "100"
                    }
                }
            },
            "expected_subprocess_calls": [
                call("service aaastatsd start", shell=True),
            ],
        }
    ]
]
