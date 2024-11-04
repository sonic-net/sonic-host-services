from unittest.mock import call

"""
    hostcfgd test vectors
"""
HOSTCFG_DAEMON_INIT_CFG_DB = {
    "FEATURE": {},
    "AAA": {},
    "TACPLUS": {},
    "TACPLUS_SERVER": {},
    "RADIUS": {},
    "RADIUS_SERVER": {},
    "LDAP": {},
    "LDAP_SERVER": {},
    "PASSW_HARDENING": {},
    "SSH_SERVER": {},
    "KDUMP": {},
    "NTP": {},
    "NTP_SERVER": {},
    "LOOPBACK_INTERFACE": {},
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "old-hostname",
            "timezone": "Etc/UTC"
        }
    },
    "MGMT_INTERFACE": {},
    "MGMT_VRF_CONFIG": {},
    "SYSLOG_CONFIG": {},
    "SYSLOG_SERVER": {},
    "DNS_NAMESERVER": {}
}


HOSTCFG_DAEMON_CFG_DB = {
    "AAA": {},
    "TACPLUS": {},
    "TACPLUS_SERVER": {},
    "RADIUS": {},
    "RADIUS_SERVER": {},
    "PASSW_HARDENING": {},
    "SSH_SERVER": {},
    "KDUMP": {
        "config": {

        }
    },
    "NTP": {
        "global": {
            "vrf": "default",
            "src_intf": "eth0;Loopback0"
        }
    },
    "NTP_SERVER": {
        "0.debian.pool.ntp.org": {}
    },
    "NTP_KEY": {
        "1": {
            "value": "blahblah",
            "type": "md5"
        },
        "42": {
            "value": "theanswer",
            "type": "md5",
            "trusted": "yes"
        }
    },
    "LOOPBACK_INTERFACE": {
        "Loopback0|10.184.8.233/32": {
            "scope": "global",
            "family": "IPv4"
        }
    },
    "DEVICE_METADATA": {
        "localhost": {
            "subtype": "DualToR",
            "type": "ToRRouter",
            "hostname": "SomeNewHostname",
            "timezone": "Europe/Kyiv"
        }
    },
    "MGMT_INTERFACE": {
        "eth0|1.2.3.4/24": {}
    },
    "MGMT_VRF_CONFIG": {
        "vrf_global": {
            'mgmtVrfEnabled': 'true'
        }
    },
    "DNS_NAMESERVER": {
        "1.1.1.1": {}
    },
}
