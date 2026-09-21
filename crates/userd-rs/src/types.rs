//! Types and data structures for userd daemon

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::LazyLock;
use std::time::Instant;

/// Constants for database tables
pub const LOCAL_USER_TABLE: &str = "LOCAL_USER";
pub const LOCAL_ROLE_SECURITY_POLICY_TABLE: &str = "LOCAL_ROLE_SECURITY_POLICY";
pub const DEVICE_METADATA_TABLE: &str = "DEVICE_METADATA";
pub const DEVICE_METADATA_LOCALHOST_KEY: &str = "localhost";
pub const LOCAL_USER_MANAGEMENT_FIELD: &str = "local_user_management";

/// PAM faillock configuration paths
pub const PAM_FAILLOCK_CONF: &str = "/etc/security/faillock.conf";
pub const PAM_FAILLOCK_TEMPLATE: &str = "/usr/share/sonic/templates/faillock.conf.j2";

/// PAM auth faillock configuration (for common-auth integration)
pub const PAM_AUTH_FAILLOCK_CONF: &str = "/etc/pam.d/common-auth-faillock";
pub const PAM_AUTH_FAILLOCK_TEMPLATE: &str = "/usr/share/sonic/templates/common-auth-faillock.j2";

/// PAM auth faillock authfail configuration (records failed attempts)
pub const PAM_AUTH_FAILLOCK_AUTHFAIL_CONF: &str = "/etc/pam.d/common-auth-faillock-authfail";
pub const PAM_AUTH_FAILLOCK_AUTHFAIL_TEMPLATE: &str =
    "/usr/share/sonic/templates/common-auth-faillock-authfail.j2";

/// Group for tracking users managed by userd
pub const MANAGED_USER_GROUP: &str = "local_mgd";

/// UID range for managed users
pub const MIN_USER_UID: u32 = 1000;
pub const MAX_USER_UID: u32 = 60000;

/// Shell paths
pub const NOLOGIN_SHELL: &str = "/usr/sbin/nologin";
pub const DEFAULT_SHELL: &str = "/bin/bash";

/// System command paths
/// Following the pattern from sonic-swss/cfgmgr/shellcmd.h
pub const CMD_USERADD: &str = "/usr/sbin/useradd";
pub const CMD_USERDEL: &str = "/usr/sbin/userdel";
pub const CMD_USERMOD: &str = "/usr/sbin/usermod";
pub const CMD_GROUPADD: &str = "/usr/sbin/groupadd";
pub const CMD_GPASSWD: &str = "/usr/bin/gpasswd";
pub const CMD_GROUPS: &str = "/usr/bin/groups";
pub const CMD_PKILL: &str = "/usr/bin/pkill";
pub const CMD_J2CLI: &str = "/usr/bin/j2";
pub const CMD_CHMOD: &str = "/usr/bin/chmod";

/// System users to exclude from management
pub static SYSTEM_USERS: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    [
        "root",
        "daemon",
        "bin",
        "sys",
        "sync",
        "games",
        "man",
        "lp",
        "mail",
        "news",
        "uucp",
        "proxy",
        "www-data",
        "backup",
        "list",
        "irc",
        "gnats",
        "nobody",
        "_apt",
        "systemd-network",
        "systemd-resolve",
        "messagebus",
        "systemd-timesync",
        "sshd",
        "redis",
        "ntp",
        "frr",
        "snmp",
    ]
    .into_iter()
    .collect()
});

/// Role to group mappings
pub static ROLE_GROUPS: LazyLock<HashMap<&'static str, Vec<&'static str>>> = LazyLock::new(|| {
    let mut m = HashMap::new();
    m.insert("administrator", vec!["sudo", "docker", "redis", "admin"]);
    m.insert("operator", vec!["users"]);
    m
});

/// Information about a user account
#[derive(Clone, Debug, Default)]
pub struct UserInfo {
    /// Username
    pub username: String,
    /// User's role (administrator, operator)
    pub role: String,
    /// Hashed password (from /etc/shadow format)
    pub password_hash: String,
    /// SSH public keys
    pub ssh_keys: Vec<String>,
    /// Whether the user account is enabled
    pub enabled: bool,
    /// User ID
    pub uid: u32,
    /// Group ID
    pub gid: u32,
    /// Home directory path
    pub home_dir: PathBuf,
    /// Login shell
    pub shell: String,
}

impl UserInfo {
    /// Compare configuration fields (excluding identity fields like uid, gid, home_dir)
    pub fn config_equals(&self, other: &UserInfo) -> bool {
        self.role == other.role
            && self.password_hash == other.password_hash
            && self.ssh_keys == other.ssh_keys
            && self.enabled == other.enabled
            && self.shell == other.shell
    }
}

impl PartialEq for UserInfo {
    fn eq(&self, other: &Self) -> bool {
        self.config_equals(other)
    }
}

/// Security policy for a role
#[derive(Clone, Debug, Default)]
pub struct SecurityPolicy {
    /// Role name
    pub role: String,
    /// Maximum login attempts before lockout
    pub max_login_attempts: i32,
}

/// State of a pending user deletion
#[derive(Debug, Clone, PartialEq)]
pub enum DeletionState {
    /// Initial state - will attempt to send SIGTERM
    Initial,
    /// SIGTERM sent to user processes, waiting for grace period
    TermSent,
    /// SIGKILL sent to user processes, waiting for processes to die
    KillSent,
    /// Maximum retries exceeded, manual intervention required
    Failed,
}

/// Pending deletion entry for tracking failed user deletions
#[derive(Debug, Clone)]
pub struct PendingDeletion {
    /// Username to delete
    pub username: String,
    /// Current state in the deletion state machine
    pub state: DeletionState,
    /// When to perform the next action
    pub next_action_time: Instant,
    /// Number of retry attempts
    pub retry_count: u32,
}
