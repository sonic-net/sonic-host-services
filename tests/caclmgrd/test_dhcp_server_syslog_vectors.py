from unittest.mock import call

"""
    caclmgrd dhcp_server syslog test vector
"""
CACLMGRD_DHCP_SERVER_SYSLOG_TEST_VECTOR = [
    [
        "DHCP_SERVER_SYSLOG_PRESENT",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "FEATURE": {
                    "dhcp_server": {
                        "auto_restart": "enabled",
                        "state": "enabled",
                    },
                },
            },
            "rule_should_be_present": True,
        }
    ],
    [
        "DHCP_SERVER_SYSLOG_ABSENT",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "subtype": "DualToR",
                        "type": "ToRRouter",
                    }
                },
                "FEATURE": {},
            },
            "rule_should_be_present": False,
        }
    ],
]
