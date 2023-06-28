'''
    hostcfgd test remote syslog configuration vector
'''

HOSTCFGD_TEST_RSYSLOG_VECTOR = {
    'initial': {
        'DEVICE_METADATA': {
            'localhost': {
                'hostname': 'radius',
            },
        },
        'SYSLOG_CONFIG': {
            'GLOBAL': {
                'format': 'standard',
                'severity': 'info'
            }
        },
        'SYSLOG_SERVER': {
            '1.1.1.1': {
                'port': 'udp',
                'vrf': 'default',
                'filter': 'exclude',
                'filter_regex': '^dsfdsdf$',
                'severity': 'debug'
            }
        },
    },
    'change_global': {
        'SYSLOG_CONFIG': {
            'GLOBAL': {
                'format': 'welf',
                'welf_firewall_name': 'harry-potter',
                'severity': 'crit'
            }
        },
    },
    'change_server': {
        'SYSLOG_SERVER': {
            '1.1.1.1': {
                'port': 'tcp',
                'vrf': 'mgmt',
                'filter': 'include',
                'filter_regex': '^dsfdsdf$',
                'severity': 'info'
            }
        },
    },
    'add_server': {
        'SYSLOG_SERVER': {
            '1.1.1.1': {
                'port': 'tcp',
                'vrf': 'mgmt',
                'filter': 'include',
                'filter_regex': '^dsfdsdf$',
                'severity': 'info'
            },
            '1.2.3.4': {
                'port': 'tcp',
                'vrf': 'default',
                'filter': 'include',
                'filter_regex': 'blabla',
                'severity': 'notice'
            }
        },
    },
    'empty_config': {
        'SYSLOG_CONFIG': {},
        'SYSLOG_SERVER': {},
    },
}
