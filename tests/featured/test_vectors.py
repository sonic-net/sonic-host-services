from unittest.mock import call

"""
    hostcfgd test vector
"""
FEATURED_TEST_VECTOR = [
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
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "pmon": {
                        "state": "enabled",
                        "delayed": "{% if 'type' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['type'] == 'SpineRouter' %}False{% else %}True{% endif %}",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    }
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "enabled"
                    },
                    "pmon": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "True",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    }
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "unmask", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "mux.service"], capture_output=True, check=True, text=True),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                    "sflow": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "disabled"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                    "sflow": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "sflow.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "sflow.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "sflow.service"], capture_output=True, check=True, text=True),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                },
            },
            "expected_config_db": {
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "disabled"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "mux.service"], capture_output=True, check=True, text=True),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "always_disabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "unmask", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "dhcp_relay.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "stop", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "mux.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "mux.service"], capture_output=True, check=True, text=True),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "dhcp_relay": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "kube",
                        "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
                    },
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
                    },
                },
            },
            "expected_config_db": {
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
                    "mux": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "set_owner": "local",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "syncd": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "syncd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd.service"], capture_output=True, check=True, text=True),

            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "syncd": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "syncd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd.service"], capture_output=True, check=True, text=True),

            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "syncd": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "syncd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "start", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd.service"], capture_output=True, check=True, text=True),

            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "syncd": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "syncd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "start", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "bgp.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "teamd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd.service"], capture_output=True, check=True, text=True),

            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "syncd": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "disabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "True",
                        "has_per_asic_scope": "False",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "syncd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                },
            },
            "enable_feature_subprocess_calls": [
                call(["sudo", "systemctl", "stop", "bgp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "bgp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "bgp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "stop", "bgp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "bgp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "bgp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "teamd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "teamd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "teamd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "teamd@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "teamd@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "teamd@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "stop", "lldp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "lldp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "lldp@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "stop", "lldp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "disable", "lldp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "mask", "lldp@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd@0.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "start", "syncd@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "enable", "syncd@1.service"], capture_output=True, check=True, text=True),
                call(["sudo", "systemctl", "unmask", "syncd@1.service"], capture_output=True, check=True, text=True),
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
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
                "FEATURE": {
                    "bgp": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "teamd": {
                        "state": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] %}disabled{% else %}enabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "lldp": {
                        "state": "enabled",
                        "delayed": "False",
                        "has_global_scope": "{% if ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['linecard']) %}False{% else %}True{% endif %}",
                        "has_per_asic_scope": "{% if not DEVICE_RUNTIME_METADATA['ETHERNET_PORTS_PRESENT'] or ('CHASSIS_METADATA' in DEVICE_RUNTIME_METADATA and DEVICE_RUNTIME_METADATA['CHASSIS_METADATA']['module_type'] in ['supervisor']) %}False{% else %}True{% endif %}",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "macsec": {
                        "state": "{% if 'type' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['type'] == 'SpineRouter' and DEVICE_RUNTIME_METADATA['MACSEC_SUPPORTED'] %}enabled{% else %}disabled{% endif %}",
                        "delayed": "False",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "auto_restart": "enabled",
                        "high_mem_alert": "disabled"
                    },
                    "pmon": {
                        "state": "enabled",
                        "delayed": "{% if 'type' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['type'] == 'SpineRouter' %}False{% else %}True{% endif %}",
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
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "teamd": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "lldp": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "macsec": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    },
                    "pmon": {
                        "auto_restart": "enabled",
                        "has_global_scope": "False",
                        "has_per_asic_scope": "True",
                        "delayed": "False",
                        "high_mem_alert": "disabled",
                        "state": "enabled"
                    }
                },
            },
            "enable_feature_subprocess_calls": [
                call(['sudo', 'systemctl', 'unmask', 'bgp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'bgp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'bgp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'bgp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'bgp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'bgp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'teamd@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'teamd@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'teamd@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'teamd@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'teamd@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'teamd@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'mask', 'lldp.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'disable', 'lldp.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'stop', 'lldp.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'lldp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'lldp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'lldp@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'lldp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'lldp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'lldp@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'macsec@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'macsec@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'macsec@0.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'macsec@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'macsec@1.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'macsec@1.service'], capture_output=True, check=True, text=True)
            ],
            "daemon_reload_subprocess_call": [
                call(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True, text=True),
            ],
            "popen_attributes": {
                'communicate.return_value': ('output', 'error')
            },
        },
    ]
]

FEATURE_DAEMON_CFG_DB = {
    "FEATURE": {
        "dhcp_relay": {
            "auto_restart": "enabled",
            "has_global_scope": "True",
            "has_per_asic_scope": "False",
            "delayed": "False",
            "high_mem_alert": "disabled",
            "set_owner": "kube",
            "state": "{% if not (DEVICE_METADATA is defined and DEVICE_METADATA['localhost'] is defined and DEVICE_METADATA['localhost']['type'] is defined and DEVICE_METADATA['localhost']['type'] != 'ToRRouter') %}enabled{% else %}disabled{% endif %}"
        },
        "mux": {
            "auto_restart": "enabled",
            "has_global_scope": "True",
            "has_per_asic_scope": "False",
            "delayed": "False",
            "high_mem_alert": "disabled",
            "set_owner": "local",
            "state": "{% if 'subtype' in DEVICE_METADATA['localhost'] and DEVICE_METADATA['localhost']['subtype'] == 'DualToR' %}enabled{% else %}always_disabled{% endif %}"
        },
    },
    "DEVICE_METADATA": {
        "localhost": {
            "subtype": "DualToR",
            "type": "ToRRouter",
            "hostname": "SomeNewHostname",
            "timezone": "Europe/Kyiv"
        }
    },
    "DEVICE_RUNTIME_METADATA": {
        "ETHERNET_PORTS_PRESENT":True
    }
}
