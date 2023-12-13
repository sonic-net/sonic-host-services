from unittest.mock import call

"""
    caclmgrd internal docker ip traffic test vector
"""
CACLMGRD_INTERNAL_DOCKER_IP_TEST_VECTOR = [
    [
        "Allow internal docker traffic",
        {
            "return": [
                ['ip', 'netns', 'exec', 'asic0', 'iptables', '-A', 'INPUT', '-s', '1.1.1.1/32', '-d', '1.1.1.1/32', '-j', 'ACCEPT'],
                ['ip', 'netns', 'exec', 'asic0', 'ip6tables', '-A', 'INPUT', '-s', 'fd::01/128', '-d', 'fd::01/128', '-j', 'ACCEPT'],
                ['ip', 'netns', 'exec', 'asic0', 'iptables', '-A', 'INPUT', '-s', '2.2.2.2/32', '-d', '1.1.1.1/32', '-j', 'ACCEPT'],
                ['ip', 'netns', 'exec', 'asic0', 'ip6tables', '-A', 'INPUT', '-s', 'fd::02/128', '-d', 'fd::01/128', '-j', 'ACCEPT']
            ]
        }
    ]
]
