"""
caclmgrd FRR daemon loopback ACL test vector.

The test driver re-uses the standard CONFIG_DB skeleton; the FRR loopback
rules are static (independent of CONFIG_DB content), so a minimal config is
sufficient to exercise the generator.
"""

CACLMGRD_FRR_LOOPBACK_ACL_TEST_VECTOR = [
    [
        "FRR daemon loopback ACL rules - default namespace",
        {
            "config_db": {
                "DEVICE_METADATA": {
                    "localhost": {
                        "type": "ToRRouter",
                    }
                },
                "FEATURE": {},
            },
        }
    ]
]
