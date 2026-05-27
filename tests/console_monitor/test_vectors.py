"""
Test vectors for consoled tests.

Contains test configuration data following SONiC CONFIG_DB schema:
- CONSOLE_SWITCH table: Feature enable/disable control
- CONSOLE_PORT table: Per-port configuration (baud_rate, remote_device, flow_control)
"""

# ============================================================
# CONSOLE_SWITCH table test data
# ============================================================

# console_mgmt entry - feature enabled
CONSOLE_SWITCH_ENABLED = {
    "console_mgmt": {
        "enabled": "yes"
    }
}

# console_mgmt entry - feature disabled
CONSOLE_SWITCH_DISABLED = {
    "console_mgmt": {
        "enabled": "no"
    }
}

# controlled_device entry for DTE side - enabled
CONTROLLED_DEVICE_ENABLED = {
    "controlled_device": {
        "enabled": "yes"
    }
}

# controlled_device entry for DTE side - disabled
CONTROLLED_DEVICE_DISABLED = {
    "controlled_device": {
        "enabled": "no"
    }
}


# ============================================================
# CONSOLE_PORT table test data
# ============================================================

# Three console ports configuration
CONSOLE_PORT_3_LINKS = {
    "1": {
        "baud_rate": "9600",
        "remote_device": "switch-01",
        "flow_control": "0"
    },
    "2": {
        "baud_rate": "115200",
        "remote_device": "switch-02",
        "flow_control": "1"
    },
    "3": {
        "baud_rate": "9600",
        "remote_device": "router-01",
        "flow_control": "0"
    }
}

# Single console port configuration
CONSOLE_PORT_SINGLE = {
    "1": {
        "baud_rate": "9600",
        "remote_device": "device-01",
        "flow_control": "0"
    }
}

# Empty console port configuration
CONSOLE_PORT_EMPTY = {}


# ============================================================
# Complete CONFIG_DB test scenarios
# ============================================================

# Scenario: DCE service with 3 console links enabled
DCE_3_LINKS_ENABLED_CONFIG_DB = {
    "CONSOLE_SWITCH": CONSOLE_SWITCH_ENABLED,
    "CONSOLE_PORT": CONSOLE_PORT_3_LINKS,
}

# Scenario: DCE service with feature disabled
DCE_FEATURE_DISABLED_CONFIG_DB = {
    "CONSOLE_SWITCH": CONSOLE_SWITCH_DISABLED,
    "CONSOLE_PORT": CONSOLE_PORT_3_LINKS,
}

# Scenario: DCE service with no ports configured
DCE_NO_PORTS_CONFIG_DB = {
    "CONSOLE_SWITCH": CONSOLE_SWITCH_ENABLED,
    "CONSOLE_PORT": CONSOLE_PORT_EMPTY,
}

# Scenario: DTE service enabled
DTE_ENABLED_CONFIG_DB = {
    "CONSOLE_SWITCH": CONTROLLED_DEVICE_ENABLED,
}

# Scenario: DTE service disabled
DTE_DISABLED_CONFIG_DB = {
    "CONSOLE_SWITCH": CONTROLLED_DEVICE_DISABLED,
}


# ============================================================
# Test vectors for parameterized tests
# ============================================================

DCE_TEST_VECTOR = [
    # (test_name, config_db, expected_proxy_count)
    ("DCE_3_Links_Enabled", DCE_3_LINKS_ENABLED_CONFIG_DB, 3),
    ("DCE_Feature_Disabled", DCE_FEATURE_DISABLED_CONFIG_DB, 0),
    ("DCE_No_Ports", DCE_NO_PORTS_CONFIG_DB, 0),
]

DTE_TEST_VECTOR = [
    # (test_name, config_db, expected_heartbeat_enabled)
    ("DTE_Enabled", DTE_ENABLED_CONFIG_DB, True),
    ("DTE_Disabled", DTE_DISABLED_CONFIG_DB, False),
]


# ============================================================
# /proc/cmdline test data for DTE
# ============================================================

PROC_CMDLINE_SINGLE_CONSOLE = "BOOT_IMAGE=/boot/vmlinuz console=ttyS0,9600n8"
PROC_CMDLINE_MULTIPLE_CONSOLE = "BOOT_IMAGE=/boot/vmlinuz console=tty0 console=ttyS1,115200"
PROC_CMDLINE_NO_BAUD = "BOOT_IMAGE=/boot/vmlinuz console=ttyS0"
PROC_CMDLINE_NO_CONSOLE = "BOOT_IMAGE=/boot/vmlinuz root=/dev/sda1"
