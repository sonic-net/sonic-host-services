"""
    caclmgrd BGP-to-FW-VLAN-SVI test vectors
"""
CACLMGRD_BGP_FW_VLAN_SVI_TEST_VECTOR = [
    [
        "FW_BT0_DEVICE_BLOCKS_BGP_FROM_VLAN_INTERFACE",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "mgmt_type": "FairWater",
                        "type": "BackEndToRRouter",
                    }
                },
            },
            "return": [
                ["ip", "netns", "exec", "", "iptables", "-A", "INPUT", "-i", "Vlan+", "-p", "tcp", "--dport", "179", "-j", "DROP"],
                ["ip", "netns", "exec", "", "ip6tables", "-A", "INPUT", "-i", "Vlan+", "-p", "tcp", "--dport", "179", "-j", "DROP"],
            ],
        }
    ],
    [
        "NON_FW_DEVICE_NO_BGP_SVI_RULES",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "ToRRouter",
                    }
                },
            },
            "return": [],
        }
    ],
    [
        "FAIRWATER_BUT_NOT_BT0_NO_BGP_SVI_RULES",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "mgmt_type": "FairWater",
                        "type": "ToRRouter",
                    }
                },
            },
            "return": [],
        }
    ],
    [
        "FW_BT0_DEVICE_NO_VLAN_INTERFACE_STILL_BLOCKS",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "mgmt_type": "FairWater",
                        "type": "BackEndToRRouter",
                    }
                },
                "VLAN_INTERFACE": {},
            },
            "return": [
                ["ip", "netns", "exec", "", "iptables", "-A", "INPUT", "-i", "Vlan+", "-p", "tcp", "--dport", "179", "-j", "DROP"],
                ["ip", "netns", "exec", "", "ip6tables", "-A", "INPUT", "-i", "Vlan+", "-p", "tcp", "--dport", "179", "-j", "DROP"],
            ],
        }
    ],
]
