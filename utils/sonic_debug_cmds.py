"""SONiC-supported commands for debug_info.py.

Switches running SONiC support a different set of commands to collect
debug artifacts. This module contains those commands.
"""

BOARD_TYPE_CMD = ""
REDIS_LIST_PC_CMD = ('docker exec -i database redis-cli -h localhost '
                     '-n 4 --raw KEYS \'*PORTCHANNEL\\|PortChannel*\'')
TEAMD_CTL_CMD = 'docker exec -i teamd teamdctl {} state dump'

CMN_CMDS = [
    "cp -r /var/log {}",
    "[ -d /var/core ] && cp -r /var/core {}",
    "cp -r /mnt/obfl_mnt_dir {}",
    "show version > {}/version.txt",
    "ip -6 route > {}/routing.txt",
    "ip neigh >> {}/routing.txt",
    "ip route >> {}/routing.txt",
    "netstat -tplnaW | grep 9339 >> {}/routing.txt",
    "netstat -tplnaW | grep 9559 >> {}/routing.txt",
    "ip link >> {}/routing.txt",
]

COUNTER_CMDS = [
    "top -b -n 1 -w 500 > {}/top.txt",
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 2 -y > {}/counter_db.json'),
]

DB_CMDS = [
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 0 -y > {}/appl_db.json'),
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 1 -y > {}/asic_db.json'),
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 4 -y > {}/config_db.json'),
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 6 -y > {}/state_db.json'),
    ('docker exec -i database '
     'redis-dump -H 127.0.0.1 -p 6379 -d 14 -y > {}/appl_state_db.json'),
    ('docker exec -i database '
     'redis-cli -n 1 hgetall VIDTORID > {}/vidtorid.txt'),
]

SDK_BASE_CMD = ''
SDK_FILE_PREFIX = ''

def get_sdk_artifact_dir():
    return "/sdk"

def get_sdk_cmds(board_type):
    return []

def get_sdk_files(board_type):
    return []
