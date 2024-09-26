from unittest.mock import call
import subprocess

"""
    caclmgrd icmpv6 conntrack rules test vector
"""
CACLMGRD_ICMPV6_CT_RULE_TEST_VECTOR = [
    [
        "ICMPv6 conntrack rules test",
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
            "return": [
            ],
        }
    ]
]
