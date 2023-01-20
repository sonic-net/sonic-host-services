from unittest.mock import call

"""
    caclmgrd ip2me block test vector
"""
CACLMGRD_IP2ME_TEST_VECTOR = [
    [
        "Only MGMT interface - default rules",
        {
            "config_db": {
                "MGMT_INTERFACE": {
                    "eth0|172.18.0.100/24": {
                        "gwaddr": "172.18.0.1"
                    }
                },
                "LOOPBACK_INTERFACE": {},
                "VLAN_INTERFACE": {},
                "PORTCHANNEL_INTERFACE": {},
                "INTERFACE": {},
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
            ],
        },
    ],
    [
        "Layer-3 loopback interfaces - block access",
        {
            "config_db": {
                "LOOPBACK_INTERFACE": {
                    "Loopback0|10.10.10.10/32": {},
                },
                "VLAN_INTERFACE": {},
                "PORTCHANNEL_INTERFACE": {
                    "PortChannel0001|10.10.11.10/32": {},
                },
                "INTERFACE": {
                    "Ethernet0|10.10.12.10/32": {}
                },
                "MGMT_INTERFACE": {
                    "eth0|172.18.0.100/24": {
                        "gwaddr": "172.18.0.1"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-d', '10.10.10.10/32', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-d', '10.10.11.10/32', '-j', 'DROP'],
                ['iptables', '-A', 'INPUT', '-d', '10.10.12.10/32', '-j', 'DROP'],
            ],
        },
    ],
    [
        "One VLAN interface, /24, we are .1",
        {
            "config_db": {
                "MGMT_INTERFACE": {
                    "eth0|172.18.0.100/24": {
                        "gwaddr": "172.18.0.1"
                    }
                },
                "LOOPBACK_INTERFACE": {},
                "VLAN_INTERFACE": {
                    "Vlan110|10.10.11.1/24": {},
                },
                "PORTCHANNEL_INTERFACE": {},
                "INTERFACE": {},
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['iptables', '-A', 'INPUT', '-d', '10.10.11.1/32', '-j', 'DROP'],
            ],
        },
    ],
    [
        "One interface of each type, IPv6, /64 - block all interfaces but MGMT",
        {
            "config_db": {
                "LOOPBACK_INTERFACE": {
                    "Loopback0|2001:db8:10::/64": {},
                },
                "VLAN_INTERFACE": {
                    "Vlan110|2001:db8:11::/64": {},
                },
                "PORTCHANNEL_INTERFACE": {
                    "PortChannel0001|2001:db8:12::/64": {},
                },
                "INTERFACE": {
                    "Ethernet0|2001:db8:13::/64": {}
                },
                "MGMT_INTERFACE": {
                    "eth0|2001:db8:200::200/64": {
                        "gwaddr": "2001:db8:200::100"
                    }
                },
                "DEVICE_METADATA": {
                    "localhost": {
                    }
                },
                "FEATURE": {},
            },
            "return": [
                ['ip6tables', '-A', 'INPUT', '-d', '2001:db8:10::/128', '-j', 'DROP'],
                ['ip6tables', '-A', 'INPUT', '-d', '2001:db8:11::1/128', '-j', 'DROP'],
                ['ip6tables', '-A', 'INPUT', '-d', '2001:db8:12::/128', '-j', 'DROP'],
                ['ip6tables', '-A', 'INPUT', '-d', '2001:db8:13::/128', '-j', 'DROP']
            ],  
        },
    ]
]
