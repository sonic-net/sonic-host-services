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
    "PASSW_HARDENING": {},
    "KDUMP": {},
    "NTP": {},
    "NTP_SERVER": {},
    "LOOPBACK_INTERFACE": {},
    "DEVICE_METADATA": {
        "localhost": {
            "hostname": "old-hostname"
        }
    },
    "MGMT_INTERFACE": {},
    "MGMT_VRF_CONFIG": {}
}


HOSTCFG_DAEMON_CFG_DB = {
    "AAA": {},
    "TACPLUS": {},
    "TACPLUS_SERVER": {},
    "RADIUS": {},
    "RADIUS_SERVER": {},
    "PASSW_HARDENING": {},
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
            "hostname": "SomeNewHostname"
        }
    },
    "MGMT_INTERFACE": {
        "eth0|1.2.3.4/24": {}
    },
    "MGMT_VRF_CONFIG": {
        "vrf_global": {
            'mgmtVrfEnabled': 'true'
        }
    }
}
