'''
    hostcfgd test logging configuration vector
'''

HOSTCFGD_TEST_LOGGING_VECTOR = {
    'initial': {
        'DEVICE_METADATA': {
            'localhost': {
                'hostname': 'logrotate',
            },
        },
        'LOGGING': {
            'syslog': {
                'disk_percentage': '',
                'frequency': 'daily',
                'max_number': '20',
                'size': '10.0'
            },
            'debug': {
                'disk_percentage': '',
                'frequency': 'daily',
                'max_number': '10',
                'size': '20.0'
            }
        },
        "SSH_SERVER": {
            "POLICIES" :{
                "max_sessions": "100"
            }
        }
    },
    'modified': {
        'LOGGING': {
            'syslog': {
                'disk_percentage': '',
                'frequency': 'weekly',
                'max_number': '100',
                'size': '20.0'
            },
            'debug': {
                'disk_percentage': '',
                'frequency': 'weekly',
                'max_number': '20',
                'size': '100.0'
            }
        }
    }
}
