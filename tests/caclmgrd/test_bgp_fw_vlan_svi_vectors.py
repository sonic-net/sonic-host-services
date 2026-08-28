"""
    caclmgrd BGP-to-FW-VLAN-SVI test vectors
"""
CACLMGRD_BGP_FW_VLAN_SVI_TEST_VECTOR = [
    [
        "FW_BT0_DEVICE_BLOCKS_BGP_TO_SVI",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "mgmt_type": "FairWater",
                        "type": "BackEndToRRouter",
                    }
                },
                "VLAN_INTERFACE": {
                    "Vlan1000|10.10.10.1/24": {},
                    "Vlan1000|fc00:10:10:10::1/64": {},
                },
            },
            "return": [
                ["iptables", "-A", "INPUT", "-d", "10.10.10.1", "-p", "tcp", "--dport", "179", "-j", "DROP"],
                ["ip6tables", "-A", "INPUT", "-d", "fc00:10:10:10::1", "-p", "tcp", "--dport", "179", "-j", "DROP"],
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
                "VLAN_INTERFACE": {
                    "Vlan1000|10.10.10.1/24": {},
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
                "VLAN_INTERFACE": {
                    "Vlan1000|10.10.10.1/24": {},
                },
            },
            "return": [],
        }
    ],
    [
        "FW_BT0_NO_VLAN_INTERFACE_NO_RULES",
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
            "return": [],
        }
    ],
]
