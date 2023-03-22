from unittest.mock import call

"""
    hostcfgd test vector
"""
HOSTCFGD_TEST_VECTOR = [
    [
        "DualTorCase",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
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
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "enabled"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "unmask", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "enable", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "start", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "unmask", "mux.service"]),
                call(["sudo", "systemctl", "enable", "mux.service"]),
                call(["sudo", "systemctl", "start", "mux.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.timer"]),
                call(["sudo", "systemctl", "enable", "telemetry.timer"]),
                call(["sudo", "systemctl", "start", "telemetry.timer"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "SingleToRCase",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "ToR",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
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
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                    "sflow": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_enabled"
                    },
                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "disabled"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                    "sflow": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "mux.service"]),
                call(["sudo", "systemctl", "disable", "mux.service"]),
                call(["sudo", "systemctl", "mask", "mux.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.timer"]),
                call(["sudo", "systemctl", "enable", "telemetry.timer"]),
                call(["sudo", "systemctl", "start", "telemetry.timer"]),
                call(["sudo", "systemctl", "unmask", "sflow.service"]),
                call(["sudo", "systemctl", "enable", "sflow.service"]),
                call(["sudo", "systemctl", "start", "sflow.service"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "T1Case",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "T1",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
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
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "disabled"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "mux.service"]),
                call(["sudo", "systemctl", "disable", "mux.service"]),
                call(["sudo", "systemctl", "mask", "mux.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.timer"]),
                call(["sudo", "systemctl", "enable", "telemetry.timer"]),
                call(["sudo", "systemctl", "start", "telemetry.timer"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "SingleToRCase_DHCP_Relay_Enabled",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "ToR",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "unmask", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "enable", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "start", "dhcp_relay.service"]),
                call(["sudo", "systemctl", "stop", "mux.service"]),
                call(["sudo", "systemctl", "disable", "mux.service"]),
                call(["sudo", "systemctl", "mask", "mux.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.service"]),
                call(["sudo", "systemctl", "unmask", "telemetry.timer"]),
                call(["sudo", "systemctl", "enable", "telemetry.timer"]),
                call(["sudo", "systemctl", "start", "telemetry.timer"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "DualTorCaseWithNoSystemCalls",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
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
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "enabled"
                    },
                    "telemetry": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "True",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "enabled",
                        "status": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('enabled', 'error')
            },
        }
    ],
    [
        "Chassis_Supervisor_PACKET",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "supervisor",
                        "chassis_type": "packet"
                        },
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },


                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp.service"]),
                call(["sudo", "systemctl", "disable", "bgp.service"]),
                call(["sudo", "systemctl", "mask", "bgp.service"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "Chassis_Supervisor_VOQ",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "supervisor",
                        "chassis_type": "voq"
                        },
                    "ETHERNET_PORTS_PRESENT":False
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },


                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp.service"]),
                call(["sudo", "systemctl", "disable", "bgp.service"]),
                call(["sudo", "systemctl", "mask", "bgp.service"]),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "Chassis_LineCard_VOQ",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "linecard",
                        "chassis_type": "voq"
                        },
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },


                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "start", "bgp.service"]),
                call(["sudo", "systemctl", "enable", "bgp.service"]),
                call(["sudo", "systemctl", "unmask", "bgp.service"]),
                call(["sudo", "systemctl", "start", "teamd.service"]),
                call(["sudo", "systemctl", "enable", "teamd.service"]),
                call(["sudo", "systemctl", "unmask", "teamd.service"]),
 
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "Chassis_LineCard_Packet",
        {
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "linecard",
                        "chassis_type": "packet"
                        },
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },


                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "start", "bgp.service"]),
                call(["sudo", "systemctl", "enable", "bgp.service"]),
                call(["sudo", "systemctl", "unmask", "bgp.service"]),
                call(["sudo", "systemctl", "start", "teamd.service"]),
                call(["sudo", "systemctl", "enable", "teamd.service"]),
                call(["sudo", "systemctl", "unmask", "teamd.service"]),
 
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "Chassis_Supervisor_PACKET_multinpu",
        {
            "num_npu": 2,
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "supervisor",
                        "chassis_type": "packet"
                        },
                    "ETHERNET_PORTS_PRESENT":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },


                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp@0.service"]),
                call(["sudo", "systemctl", "disable", "bgp@0.service"]),
                call(["sudo", "systemctl", "mask", "bgp@0.service"]),
                call(["sudo", "systemctl", "stop", "bgp@1.service"]),
                call(["sudo", "systemctl", "disable", "bgp@1.service"]),
                call(["sudo", "systemctl", "mask", "bgp@1.service"]),
                call(["sudo", "systemctl", "start", "teamd@0.service"]),
                call(["sudo", "systemctl", "enable", "teamd@0.service"]),
                call(["sudo", "systemctl", "unmask", "teamd@0.service"]),
                call(["sudo", "systemctl", "start", "teamd@1.service"]),
                call(["sudo", "systemctl", "enable", "teamd@1.service"]),
                call(["sudo", "systemctl", "unmask", "teamd@1.service"]),
                call(["sudo", "systemctl", "stop", "lldp@0.service"]),
                call(["sudo", "systemctl", "disable", "lldp@0.service"]),
                call(["sudo", "systemctl", "mask", "lldp@0.service"]),
                call(["sudo", "systemctl", "stop", "lldp@1.service"]),
                call(["sudo", "systemctl", "disable", "lldp@1.service"]),
                call(["sudo", "systemctl", "mask", "lldp@1.service"]),
 
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ],
    [
        "Chassis_LineCard_VOQ_multinpu",
        {
            "num_npu": 2,
            "device_runtime_metadata": {
                "DEVICE_RUNTIME_METADATA": {
                    "CHASSIS_METADATA": {
                        "module_type": "linecard",
                        "chassis_type": "voq"
                        },
                    "ETHERNET_PORTS_PRESENT":True,
                    "MACSEC_SUPPORTED":True
                    }
                },
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "SpineRouter",
                    }
                },
                "KDUMP": {
                    "config": {
                        "enabled": "false",
                        "num_dumps": "3",
                        "memory": "0M-2G:256M,2G-4G:320M,4G-8G:384M,8G-:448M"
                    }
                },
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "has_timer": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "macsec": {
                        "state": "{% if 'type' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['type'] == 'SpineRouter' and DEVICE_RUNTIME_METADATA['MACSEC_SUPPORTED'] %}enabled{% else %}disabled{% endif %}",
                        "has_timer": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    }
                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "bgp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "macsec": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "has_timer": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    }
                },
            },
            "enable_feature_subprocess_calls": [
                call(['sudo', 'systemctl', 'unmask', 'bgp@0.service']),
                call(['sudo', 'systemctl', 'enable', 'bgp@0.service']),
                call(['sudo', 'systemctl', 'start', 'bgp@0.service']),
                call(['sudo', 'systemctl', 'unmask', 'bgp@1.service']),
                call(['sudo', 'systemctl', 'enable', 'bgp@1.service']),
                call(['sudo', 'systemctl', 'start', 'bgp@1.service']),
                call(['sudo', 'systemctl', 'unmask', 'teamd@0.service']),
                call(['sudo', 'systemctl', 'enable', 'teamd@0.service']),
                call(['sudo', 'systemctl', 'start', 'teamd@0.service']),
                call(['sudo', 'systemctl', 'unmask', 'teamd@1.service']),
                call(['sudo', 'systemctl', 'enable', 'teamd@1.service']),
                call(['sudo', 'systemctl', 'start', 'teamd@1.service']),
                call(['sudo', 'systemctl', 'unmask', 'lldp.service']),
                call(['sudo', 'systemctl', 'enable', 'lldp.service']),
                call(['sudo', 'systemctl', 'start', 'lldp.service']),
                call(['sudo', 'systemctl', 'unmask', 'lldp@0.service']),
                call(['sudo', 'systemctl', 'enable', 'lldp@0.service']),
                call(['sudo', 'systemctl', 'start', 'lldp@0.service']),
                call(['sudo', 'systemctl', 'unmask', 'lldp@1.service']),
                call(['sudo', 'systemctl', 'enable', 'lldp@1.service']),
                call(['sudo', 'systemctl', 'start', 'lldp@1.service']),
                call(['sudo', 'systemctl', 'unmask', 'macsec@0.service']),
                call(['sudo', 'systemctl', 'enable', 'macsec@0.service']),
                call(['sudo', 'systemctl', 'start', 'macsec@0.service']),
                call(['sudo', 'systemctl', 'unmask', 'macsec@1.service']),
                call(['sudo', 'systemctl', 'enable', 'macsec@1.service']),
                call(['sudo', 'systemctl', 'start', 'macsec@1.service'])
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"]),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ]
 
]

HOSTCFG_DAEMON_INIT_CFG_DB = {
    "FEATURE": {},
    "AAA": {},
    "TACPLUS": {},
    "TACPLUS_SERVER": {},
    "RADIUS": {},
    "RADIUS_SERVER": {},
    "PASSW_HARDENING": {},
    "KDUMP": {},
    "NTP": {},
    "NTP_SERVER": {},
    "LOOPBACK_INTERFACE": {},
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "old-hostname",
            "timezone": "Etc/UTC"
        }
    },
    "MGMT_INTERFACE": {},
    "MGMT_VRF_CONFIG": {}
}


HOSTCFG_DAEMON_CFG_DB = {
    "FEATURE": {
        "dhcp_relay": {
            "auto_restart": "enabled",
            "has_global_scope": "True",
            "has_per_asic_scope": "False",
            "has_timer": "False",
            "high_mem_alert": "disabled",
            "set_owner": "kube",
            "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
        },
        "mux": {
            "auto_restart": "enabled",
            "has_global_scope": "True",
            "has_per_asic_scope": "False",
            "has_timer": "False",
            "high_mem_alert": "disabled",
            "set_owner": "local",
            "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
        },
        "telemetry": {
            "auto_restart": "enabled",
            "has_global_scope": "True",
            "has_per_asic_scope": "False",
            "has_timer": "True",
            "high_mem_alert": "disabled",
            "set_owner": "kube",
            "state": "enabled",
            "status": "enabled"
        },
    },
    "AAA": {},
    "TACPLUS": {},
    "TACPLUS_SERVER": {},
    "RADIUS": {},
    "RADIUS_SERVER": {},
    "PASSW_HARDENING": {},
    "KDUMP": {
        "config": {

        }
    },
    "NTP": {
        "global": {
            "vrf": "default",
            "src_intf": "eth0;Loopback0"
        }
    },
    "NTP_SERVER": {
        "0.debian.pool.ntp.org": {}
    },
    "LOOPBACK_INTERFACE": {
        "Loopback0|10.184.8.233/32": {
            "scope": "global",
            "family": "IPv4"
        }
    },
    "DEVICE_METADATA": {
        "localhost": {
            "subtype": "DualToR",
            "type": "ToRRouter",
            "hostname": "SomeNewHostname",
            "timezone": "Europe/Kyiv"
        }
    },
    "MGMT_INTERFACE": {
        "eth0|1.2.3.4/24": {}
    },
    "MGMT_VRF_CONFIG": {
        "vrf_global": {
            'mgmtVrfEnabled': 'true'
        }
    }
}
