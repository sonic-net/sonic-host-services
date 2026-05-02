//! User Manager - Core user management functionality
//!
//! Handles user creation, deletion, updates, and consistency checking.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use tracing::{debug, error, info, warn};

use crate::cmd;
use crate::commands::{
    run_gpasswd_remove, run_groupadd, run_groups, run_useradd, run_userdel, run_usermod_add_group,
    run_usermod_password, run_usermod_shell, GpasswdResult,
};
use crate::error::{UserdError, UserdResult};
use crate::system::{CommandExecutorTrait, SystemFunctionsTrait};
use crate::templates::render_pam_faillock;
use crate::types::*;

/// User Manager - handles all user management operations
pub struct UserManager<C: CommandExecutorTrait, S: SystemFunctionsTrait> {
    /// Command executor for running system commands
    cmd_executor: C,
    /// System functions for file/user operations
    sys_funcs: S,
    /// Cached users from CONFIG_DB
    users: HashMap<String, UserInfo>,
    /// Cached security policies
    security_policies: HashMap<String, SecurityPolicy>,
    /// Whether local user management feature is enabled
    feature_enabled: bool,
    /// Whether initial sync done
    initial_read_done: bool,
    /// Pending user deletions (username -> PendingDeletion)
    pending_deletions: HashMap<String, crate::types::PendingDeletion>,
}

impl<C: CommandExecutor, S: SystemFunctions> UserManager<C, S> {
    /// Create a new UserManager instance
    pub fn new(cmd_executor: C, sys_funcs: S) -> Self {
        UserManager {
            cmd_executor,
            sys_funcs,
            users: HashMap::new(),
            security_policies: HashMap::new(),
            feature_enabled: false,
            initial_read_done: false,
            pending_deletions: HashMap::new(),
        }
    }

    /// Load configuration from CONFIG_DB
    pub fn handle_feature_config(&mut self) -> UserdResult<()> {
        if !self.is_feature_enabled() {
            info!("Local user management feature is disabled");
            self.cleanup_managed_state();
            return Ok(());
        }

        info!(
            "Cache has {} users and {} security policies",
            self.users.len(),
            self.security_policies.len()
        );

        // Update all cached users in the system
        for (username, user) in &self.users.clone() {
            let existing_users = self.get_existing_users();
            if existing_users.contains_key(username) {
                self.update_user(username, user)?;
            } else {
                self.create_user(username, user)?;
            }
        }

        if let Err(e) = self.perform_consistency_check() {
            error!("Failed initial consistency check: {}", e);
        }
        if let Err(e) = self.update_security_policies() {
            error!("Failed to update security policies: {}", e);
        }
        Ok(())
    }

    /// Parse SSH keys from JSON string
    fn parse_ssh_keys(&self, value: &str) -> Vec<String> {
        if value.is_empty() {
            return Vec::new();
        }

        // SSH keys are stored as JSON array
        match serde_json::from_str::<Vec<String>>(value) {
            Ok(keys) => keys.into_iter().filter(|k| !k.is_empty()).collect(),
            Err(_) => {
                // Try parsing as comma-separated string
                value
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            }
        }
    }

    /// Check if feature is enabled
    pub fn is_feature_enabled(&self) -> bool {
        self.feature_enabled
    }

    /// Get mutable reference to feature_enabled for config change handling
    pub fn set_feature_enabled(&mut self, enabled: bool) {
        self.feature_enabled = enabled;
    }

    pub fn set_initial_read_done(&mut self) {
        self.initial_read_done = true;
    }

    /// Get existing system users
    pub fn get_existing_users(&self) -> HashMap<String, UserInfo> {
        let mut users = HashMap::new();

        // Parse /etc/passwd to get all users
        let passwd_path = Path::new("/etc/passwd");
        let file = match File::open(passwd_path) {
            Ok(f) => f,
            Err(e) => {
                warn!("Failed to open /etc/passwd: {} - cannot enumerate existing users", e);
                return users;
            }
        };

        let reader = BufReader::new(file);
        for line in reader.lines().map_while(Result::ok) {
            if let Some(user) = self.parse_passwd_line(&line) {
                // Skip system users by name and users outside our UID range
                if SYSTEM_USERS.contains(user.username.as_str()) {
                    continue;
                }
                if user.uid >= MIN_USER_UID && user.uid <= MAX_USER_UID {
                    users.insert(user.username.clone(), user);
                }
            }
        }

        users
    }

    /// Parse a line from /etc/passwd
    fn parse_passwd_line(&self, line: &str) -> Option<UserInfo> {
        let parts: Vec<&str> = line.split(':').collect();
        if parts.len() < 7 {
            return None;
        }

        let username = parts[0].to_string();
        let uid: u32 = parts[2].parse().ok()?;
        let gid: u32 = parts[3].parse().ok()?;
        let home_dir = PathBuf::from(parts[5]);
        let shell = parts[6].to_string();

        // Get additional info from shadow file and groups
        let password_hash = self.get_shadow_hash(&username).unwrap_or_default();
        let role = self.determine_user_role(&username);
        let ssh_keys = self.read_user_ssh_keys(&username, &home_dir);
        let enabled = shell != NOLOGIN_SHELL;

        Some(UserInfo {
            username,
            role,
            password_hash,
            ssh_keys,
            enabled,
            uid,
            gid,
            home_dir,
            shell,
        })
    }

    /// Get password hash from /etc/shadow
    fn get_shadow_hash(&self, username: &str) -> Option<String> {
        let shadow_path = Path::new("/etc/shadow");
        let file = File::open(shadow_path).ok()?;
        let reader = BufReader::new(file);

        for line in reader.lines().map_while(Result::ok) {
            let parts: Vec<&str> = line.split(':').collect();
            if parts.len() >= 2 && parts[0] == username {
                return Some(parts[1].to_string());
            }
        }

        None
    }

    /// Determine user role based on group membership
    ///
    /// Checks each role to see if user has ALL required groups for that role.
    /// Returns empty string if no role matches.
    fn determine_user_role(&self, username: &str) -> String {
        let user_groups = self.get_user_groups(username);
        let user_groups_set: HashSet<&str> = user_groups.iter().map(|s| s.as_str()).collect();

        // Check each role to see if user has all required groups for that role
        for (role, required_groups) in ROLE_GROUPS.iter() {
            let has_all_groups = required_groups
                .iter()
                .all(|group| user_groups_set.contains(group));

            if has_all_groups {
                return role.to_string();
            }
        }

        // If no role matches, return empty string
        String::new()
    }

    /// Check if user is in a specific group
    pub fn is_user_in_group(&self, username: &str, group: &str) -> bool {
        let user_groups = self.get_user_groups(username);
        user_groups.contains(&group.to_string())
    }

    /// Get groups for a user
    fn get_user_groups(&self, username: &str) -> Vec<String> {
        run_groups(&self.cmd_executor, username)
    }

    /// Read SSH keys from user's authorized_keys file
    fn read_user_ssh_keys(&self, _username: &str, home_dir: &Path) -> Vec<String> {
        let auth_keys_path = home_dir.join(".ssh/authorized_keys");
        if let Ok(content) = fs::read_to_string(&auth_keys_path) {
            return content
                .lines()
                .filter(|l| !l.trim().is_empty() && !l.starts_with('#'))
                .map(|l| l.to_string())
                .collect();
        }
        Vec::new()
    }

    /// Check if a user is managed by userd
    pub fn is_user_managed(&self, username: &str) -> bool {
        self.is_user_in_group(username, MANAGED_USER_GROUP)
    }

    /// Create a new user
    pub fn create_user(&mut self, username: &str, user_config: &UserInfo) -> UserdResult<bool> {
        info!("Creating user: {}", username);

        // Check if user already exists
        if self.sys_funcs.getpwnam(username).is_some() {
            warn!("User {} already exists", username);
            return Ok(false);
        }

        // Validate role
        if !user_config.role.is_empty() && !ROLE_GROUPS.contains_key(user_config.role.as_str()) {
            error!("Invalid role '{}' for user {}", user_config.role, username);
            return Err(UserdError::InvalidRole(user_config.role.clone()));
        }

        // Create user with useradd - let the system assign UID automatically
        // (avoids potential conflicts with RADIUS/TACACS user creation)
        let home_dir = format!("/home/{}", username);
        let shell = if user_config.enabled {
            DEFAULT_SHELL
        } else {
            NOLOGIN_SHELL
        };

        run_useradd(&self.cmd_executor, username, &home_dir, shell)?;

        // User created successfully - now configure it
        // If configuration fails, clean up the created user
        if let Err(e) = self.setup_user_configuration(username, user_config) {
            warn!(
                "User configuration failed, cleaning up user {}: {}",
                username, e
            );
            let _ = self.delete_user(username); // Ignore timer return on cleanup
            return Err(e);
        }

        info!("Successfully created user: {}", username);
        Ok(true)
    }

    /// Setup user configuration after user creation
    ///
    /// Sets password, groups, SSH keys.
    /// Returns error if critical setup fails.
    fn setup_user_configuration(&self, username: &str, user_config: &UserInfo) -> UserdResult<()> {
        self.set_user_password(username, &user_config.password_hash)?;
        self.set_user_groups(username, &user_config.role)?;

        if !user_config.ssh_keys.is_empty() {
            self.setup_ssh_keys(username, &user_config.ssh_keys)?;
        }

        Ok(())
    }

    /// Delete a user
    /// Returns Ok((deleted, next_timer)) where:
    /// - deleted: true if user was deleted or queued for deletion
    /// - next_timer: Some(Instant) if a pending deletion was added, None otherwise
    pub fn delete_user(&mut self, username: &str) -> UserdResult<(bool, Option<Instant>)> {
        info!("Deleting user: {}", username);

        // Check if user exists
        if self.sys_funcs.getpwnam(username).is_none() {
            warn!("User {} does not exist", username);
            return Ok((false, None));
        }

        // Try to remove user with userdel
        match run_userdel(&self.cmd_executor, username) {
            Ok(_) => {
                info!("Successfully deleted user: {}", username);
                Ok((true, None))
            }
            Err(UserdError::CommandError { ref message, .. })
                if message.contains("currently used by process") =>
            {
                // User has active processes - add to pending deletion queue
                warn!(
                    "User {} has active processes. Adding to pending deletion queue.",
                    username
                );
                let next_timer = self.add_pending_deletion(username);
                Ok((true, Some(next_timer))) // Will be retried via pending deletion mechanism
            }
            Err(e) => Err(e),
        }
    }

    /// Add a user to the pending deletion queue
    /// Returns the next action time for timer scheduling
    fn add_pending_deletion(&mut self, username: &str) -> Instant {
        use crate::types::{DeletionState, PendingDeletion};

        let next_action_time = Instant::now() + Duration::from_secs(5);
        let pending = PendingDeletion {
            username: username.to_string(),
            state: DeletionState::Initial,
            next_action_time,
            retry_count: 0,
        };

        self.pending_deletions.insert(username.to_string(), pending);
        info!("Added user {} to pending deletion queue", username);
        next_action_time
    }

    /// Process pending user deletions - returns next check time
    ///
    /// This implements a state machine for gracefully deleting users with active processes:
    /// 1. Initial → Send SIGTERM → wait 5 seconds → TermSent
    /// 2. TermSent → Send SIGKILL → wait 2 seconds → KillSent
    /// 3. KillSent → Retry userdel → Success? Remove from queue : Retry (max 3 times)
    ///
    /// Returns the next time this function should be called
    pub fn process_pending_deletions(&mut self) -> Instant {
        use crate::commands::run_pkill;
        use crate::types::DeletionState;

        let now = Instant::now();
        let mut to_remove = Vec::new();

        for (username, pending) in self.pending_deletions.iter_mut() {
            // Skip if not time yet
            if now < pending.next_action_time {
                continue;
            }

            match pending.state {
                DeletionState::Initial => {
                    // Send SIGTERM to user processes
                    info!("Sending SIGTERM to processes of user {}", username);
                    match run_pkill(&self.cmd_executor, username, "TERM") {
                        Ok(true) => {
                            // Processes found and signaled, wait for them to terminate
                            pending.state = DeletionState::TermSent;
                            pending.next_action_time = now + Duration::from_secs(5);
                        }
                        Ok(false) => {
                            // No processes found, skip to retry immediately
                            info!("No processes found for user {}, retrying deletion immediately", username);
                            pending.state = DeletionState::KillSent;
                            pending.next_action_time = now; // Retry immediately
                        }
                        Err(e) => {
                            warn!("Failed to send SIGTERM to user {}: {}", username, e);
                            // Continue anyway, try SIGKILL next
                            pending.state = DeletionState::TermSent;
                            pending.next_action_time = now + Duration::from_secs(5);
                        }
                    }
                }

                DeletionState::TermSent => {
                    // Send SIGKILL to user processes
                    info!("Sending SIGKILL to processes of user {}", username);
                    match run_pkill(&self.cmd_executor, username, "KILL") {
                        Ok(true) => {
                            // Processes found and signaled, wait for them to die
                            pending.state = DeletionState::KillSent;
                            pending.next_action_time = now + Duration::from_secs(2);
                        }
                        Ok(false) => {
                            // No processes found, retry deletion immediately
                            info!("No processes found for user {}, retrying deletion immediately", username);
                            pending.state = DeletionState::KillSent;
                            pending.next_action_time = now; // Retry immediately
                        }
                        Err(e) => {
                            warn!("Failed to send SIGKILL to user {}: {}", username, e);
                            // Continue anyway, try deletion
                            pending.state = DeletionState::KillSent;
                            pending.next_action_time = now + Duration::from_secs(2);
                        }
                    }
                }

                DeletionState::KillSent => {
                    // Retry deletion
                    info!("Retrying deletion for user {}", username);
                    match run_userdel(&self.cmd_executor, username) {
                        Ok(_) => {
                            info!("Successfully deleted user {} after process termination", username);
                            to_remove.push(username.clone());
                        }
                        Err(e) => {
                            pending.retry_count += 1;
                            if pending.retry_count >= 3 {
                                error!(
                                    "Failed to delete user {} after {} retries: {}. Manual intervention required.",
                                    username, pending.retry_count, e
                                );
                                pending.state = DeletionState::Failed;
                            } else {
                                warn!("Retry {} failed for user {}: {}", pending.retry_count, username, e);
                                pending.state = DeletionState::Initial;
                                pending.next_action_time = now + Duration::from_secs(10);
                            }
                        }
                    }
                }

                DeletionState::Failed => {
                    // Already logged error, nothing more to do
                }
            }
        }

        // Remove successfully deleted users
        for username in to_remove {
            self.pending_deletions.remove(&username);
        }

        // Calculate next check time
        self.pending_deletions
            .values()
            .map(|p| p.next_action_time)
            .min()
            .unwrap_or_else(|| now + Duration::from_secs(60))
    }

    /// Unmanage a user (remove from managed group but keep account)
    /// Returns Ok(true) if user was unmanaged, Ok(false) if user wasn't managed
    pub fn unmanage_user(&self, username: &str) -> UserdResult<bool> {
        // Remove user from managed group to indicate they're no longer managed
        if self.is_user_managed(username) {
            match run_gpasswd_remove(&self.cmd_executor, username, MANAGED_USER_GROUP)? {
                GpasswdResult::Removed => {
                    info!(
                        "Removed user {} from managed group {}",
                        username, MANAGED_USER_GROUP
                    );
                    info!(
                        "Successfully unmanaged user {} (user account preserved)",
                        username
                    );
                    Ok(true)
                }
                GpasswdResult::NotMember => {
                    debug!("User {} was not in managed group", username);
                    Ok(false)
                }
            }
        } else {
            debug!("User {} is not in managed group", username);
            Ok(false)
        }
    }

    /// Update an existing user
    pub fn update_user(&self, username: &str, user_config: &UserInfo) -> UserdResult<bool> {
        // Get current user info
        let current_users = self.get_existing_users();
        let current_info = match current_users.get(username) {
            Some(info) => info,
            None => {
                error!("User {} not found for update", username);
                return Ok(false);
            }
        };

        // Create expected UserInfo with correct shell based on enabled status
        let expected_shell = if user_config.enabled {
            DEFAULT_SHELL
        } else {
            NOLOGIN_SHELL
        };

        // Check and update password
        if !user_config.password_hash.is_empty()
            && current_info.password_hash != user_config.password_hash
        {
            self.set_user_password(username, &user_config.password_hash)?;
        }

        // Check and update shell
        if current_info.shell != expected_shell {
            self.set_user_shell(username, user_config.enabled)?;
        }

        // Always ensure user is in managed group and has correct role groups
        // (even if role appears unchanged, the user might be missing from local_mgd)
        if !user_config.role.is_empty() {
            if current_info.role != user_config.role {
                info!(
                    "Changing user {} role from '{}' to '{}'",
                    username, current_info.role, user_config.role
                );
            }
            self.set_user_groups(username, &user_config.role)?;
        }

        // Check and update SSH keys
        if current_info.ssh_keys != user_config.ssh_keys {
            self.setup_ssh_keys(username, &user_config.ssh_keys)?;
        }

        info!("Updated user {}", username);
        Ok(true)
    }

    /// Set user password hash directly in /etc/shadow
    pub fn set_user_password(&self, username: &str, password_hash: &str) -> UserdResult<()> {
        debug!("Setting password for user: {}", username);
        run_usermod_password(&self.cmd_executor, username, password_hash)
    }

    /// Set user shell (enable/disable login)
    pub fn set_user_shell(&self, username: &str, enabled: bool) -> UserdResult<()> {
        let shell = if enabled {
            DEFAULT_SHELL
        } else {
            NOLOGIN_SHELL
        };

        debug!("Setting shell for user {}: {}", username, shell);
        run_usermod_shell(&self.cmd_executor, username, shell)
    }

    /// Set user groups based on role
    ///
    /// This function:
    /// 1. Ensures the managed group exists
    /// 2. Adds user to managed group (if not already)
    /// 3. Removes user from groups of other roles
    /// 4. Adds user to groups for the new role
    pub fn set_user_groups(&self, username: &str, role: &str) -> UserdResult<()> {
        // Ensure managed group exists
        if !self.ensure_managed_group_exists()? {
            error!(
                "Failed to ensure managed group exists for user {}",
                username
            );
            return Err(UserdError::CommandError {
                command: "ensure_managed_group_exists".to_string(),
                message: "Failed to ensure managed group exists".to_string(),
            });
        }

        // Get user's current groups ONCE (avoid repeated 'groups' command calls)
        let current_groups: HashSet<String> = self.get_user_groups(username).into_iter().collect();

        // Always add user to managed group first (only if not already a member)
        if !current_groups.contains(MANAGED_USER_GROUP) {
            run_usermod_add_group(&self.cmd_executor, username, MANAGED_USER_GROUP)?;
            debug!("Added user {} to managed group", username);
        } else {
            debug!("User {} already in managed group", username);
        }

        let new_role_groups = match ROLE_GROUPS.get(role) {
            Some(g) => g,
            None => {
                warn!("No groups defined for role {}", role);
                return Ok(());
            }
        };

        // Get all role-based groups that this user should NOT be in
        // (groups from other roles that are not in the new role)
        let new_role_groups_set: HashSet<&str> = new_role_groups.iter().copied().collect();
        let mut groups_to_remove: Vec<&str> = Vec::new();

        for (other_role, other_groups) in ROLE_GROUPS.iter() {
            if *other_role != role {
                for group in other_groups.iter() {
                    // Only remove if the group is not also part of the new role
                    // and user is currently in that group
                    if !new_role_groups_set.contains(group) && current_groups.contains(*group) {
                        groups_to_remove.push(group);
                    }
                }
            }
        }

        // Remove user from groups they should no longer be in
        for group in groups_to_remove {
            match run_gpasswd_remove(&self.cmd_executor, username, group) {
                Ok(GpasswdResult::Removed) => {
                    debug!("Removed user {} from group {}", username, group);
                }
                Ok(GpasswdResult::NotMember) => {
                    debug!("User {} was not in group {}", username, group);
                }
                Err(e) => {
                    warn!("Failed to remove user {} from group {}: {}", username, group, e);
                }
            }
        }

        // Add user to role-specific groups
        for group in new_role_groups.iter() {
            if !current_groups.contains(*group) {
                // Don't fail if group doesn't exist
                let _ = run_usermod_add_group(&self.cmd_executor, username, group);
                debug!("Added user {} to group {}", username, group);
            } else {
                debug!("User {} already in group {}", username, group);
            }
        }

        debug!("Updated user {} groups for role {}", username, role);
        Ok(())
    }

    /// Ensure the managed user group exists
    fn ensure_managed_group_exists(&self) -> UserdResult<bool> {
        // Check if group already exists using getgrnam
        if self.sys_funcs.getgrnam(MANAGED_USER_GROUP).is_some() {
            debug!("Managed group {} already exists", MANAGED_USER_GROUP);
            return Ok(true);
        }

        // Create the managed group
        run_groupadd(&self.cmd_executor, MANAGED_USER_GROUP)
    }

    /// Validate SSH key format
    fn is_valid_ssh_key(&self, key: &str) -> bool {
        if key.is_empty() {
            return false;
        }

        // Check if it starts with a known SSH key type
        if !key.starts_with("ssh-")
            && !key.starts_with("ecdsa-")
            && !key.contains("ed25519")
            && !key.contains("rsa")
        {
            return false;
        }

        // Check if it has at least 2 parts (type and key, comment is optional)
        let part_count = key.split_whitespace().take(3).count();
        part_count >= 2
    }

    /// Setup SSH authorized keys for a user
    pub fn setup_ssh_keys(&self, username: &str, ssh_keys: &[String]) -> UserdResult<()> {
        // Filter and validate SSH keys
        let valid_keys: Vec<&String> = ssh_keys
            .iter()
            .filter(|key| {
                if self.is_valid_ssh_key(key) {
                    true
                } else {
                    warn!("Skipping invalid SSH key for user {}", username);
                    false
                }
            })
            .collect();

        if valid_keys.is_empty() && !ssh_keys.is_empty() {
            warn!("No valid SSH keys for user {}", username);
        }

        // Get user home directory
        let passwd_entry = self
            .sys_funcs
            .getpwnam(username)
            .ok_or_else(|| UserdError::UserNotFound(username.to_string()))?;

        let home_dir = passwd_entry.dir.clone();
        let ssh_dir = home_dir.join(".ssh");
        let auth_keys_path = ssh_dir.join("authorized_keys");

        // Create .ssh directory if it doesn't exist
        if !ssh_dir.exists() {
            fs::create_dir_all(&ssh_dir).map_err(|e| UserdError::FileError {
                path: ssh_dir.to_string_lossy().to_string(),
                message: e.to_string(),
            })?;
        }

        // Set directory permissions (700)
        self.sys_funcs.chmod(&ssh_dir, 0o700)?;
        self.sys_funcs
            .chown(&ssh_dir, passwd_entry.uid, passwd_entry.gid)?;

        // Write authorized_keys file atomically using temp file + rename
        // This ensures the original file is preserved if write fails
        let content = valid_keys
            .iter()
            .map(|k| k.as_str())
            .collect::<Vec<_>>()
            .join("\n");

        let temp_path = auth_keys_path.with_extension("tmp");

        // Write to temporary file
        fs::write(&temp_path, format!("{}\n", content)).map_err(|e| UserdError::FileError {
            path: temp_path.to_string_lossy().to_string(),
            message: e.to_string(),
        })?;

        // Set permissions and ownership on temp file before rename
        self.sys_funcs.chmod(&temp_path, 0o600)?;
        self.sys_funcs
            .chown(&temp_path, passwd_entry.uid, passwd_entry.gid)?;

        // Atomic rename to final location
        fs::rename(&temp_path, &auth_keys_path).map_err(|e| UserdError::FileError {
            path: auth_keys_path.to_string_lossy().to_string(),
            message: format!("Failed to rename temp file: {}", e),
        })?;

        debug!("Setup {} SSH keys for user {}", valid_keys.len(), username);
        Ok(())
    }

    /// Perform consistency check at startup
    pub fn perform_consistency_check(&mut self) -> UserdResult<()> {
        if !self.is_feature_enabled() {
            info!("Feature disabled, skipping consistency check");
            return Ok(());
        }

        debug!("Performing startup consistency check...");

        // Get existing system users
        let system_users = self.get_existing_users();

        // Get users that should exist according to CONFIG_DB
        let config_users: HashSet<String> = self.users.keys().cloned().collect();

        // Ensure all CONFIG_DB users exist and are properly configured
        for (username, user_config) in &self.users.clone() {
            if !system_users.contains_key(username) {
                info!("Creating missing user: {}", username);
                self.create_user(username, user_config)?;
            } else {
                // Update existing user configuration
                self.update_user(username, user_config)?;
            }
        }

        // Find managed users that exist in system but not in CONFIG_DB
        // Get the members of the managed group ONCE (avoid repeated 'groups' command calls)
        let managed_group_members: HashSet<String> =
            match self.sys_funcs.getgrnam(MANAGED_USER_GROUP) {
                Some(group) => group.mem.into_iter().collect(),
                None => HashSet::new(),
            };

        let mut unmanaged_users = Vec::new();
        for username in system_users.keys() {
            // Skip if user is in CONFIG_DB
            if config_users.contains(username) {
                continue;
            }
            // Skip system users
            if SYSTEM_USERS.contains(username.as_str()) {
                continue;
            }
            // Only consider users that are managed by userd (in local_mgd group)
            if managed_group_members.contains(username) {
                unmanaged_users.push(username.clone());
            }
        }

        // Remove unmanaged users that were previously managed by userd
        for username in unmanaged_users {
            info!("Removing previously managed user: {}", username);
            let _ = self.delete_user(&username)?; // Ignore timer return during consistency check
        }

        info!("Consistency check completed");
        Ok(())
    }

    /// Update security policies
    pub fn update_security_policies(&self) -> UserdResult<()> {
        if !self.is_feature_enabled() {
            return Ok(());
        }

        render_pam_faillock(&self.security_policies)?;
        info!("Security policies updated");
        Ok(())
    }

    /// Cleanup all managed state when feature is disabled
    ///
    /// Discovers managed users from the system (via local_mgd group membership)
    /// rather than from DB, since the DB tables may have been cleared when the
    /// feature was disabled. Also resets PAM config and clears internal state.
    pub fn cleanup_managed_state(&mut self) {
        info!("Cleaning up all managed users and policies");

        // Get members of the managed user group from the system
        let managed_users = match self.sys_funcs.getgrnam(MANAGED_USER_GROUP) {
            Some(group) => group.mem,
            None => {
                // Group doesn't exist - no users to clean up
                debug!("Managed group {} does not exist, no users to clean up", MANAGED_USER_GROUP);
                vec![]
            }
        };

        if managed_users.is_empty() {
            debug!("No users in managed group");
        } else {
            info!("Unmanaging {} users from system", managed_users.len());
            for username in &managed_users {
                info!("Unmanaging user: {}", username);
                if let Err(e) = self.unmanage_user(username) {
                    warn!("Failed to unmanage user {}: {}", username, e);
                }
            }
        }

        // Reset PAM config to defaults (no policies)
        if let Err(e) = render_pam_faillock(&HashMap::new()) {
            warn!("Failed to reset PAM faillock config: {}", e);
        }

        // Clear internal state
        self.users.clear();
        self.security_policies.clear();

        info!("Managed state cleanup complete");
    }

    /// Extract the user attributes
    fn extract_user_attributes(&self, key: &str, data: &HashMap<String, String>) -> UserInfo {
        // User added or modified
        let mut user = UserInfo {
            username: key.to_string(),
            ..Default::default()
        };

        for (field, value) in data {
            match field.as_str() {
                "role" => user.role = value.clone(),
                "password_hash" => user.password_hash = value.clone(),
                "enabled" => user.enabled = value == "true" || value == "True",
                "ssh_keys" => {
                    user.ssh_keys = self.parse_ssh_keys(value);
                }
                _ => {}
            }
        }
        user
    }

    /// Handle user table change from CONFIG_DB (LOCAL_USER table)
    /// Returns Some(Instant) if timer needs to be updated, None otherwise
    pub fn handle_user_change(&mut self, key: &str, data: &HashMap<String, String>) -> UserdResult<Option<Instant>> {
        if !data.is_empty() {
            // Cancel pending deletion if user is re-added
            if let Some(pending) = self.pending_deletions.remove(key) {
                warn!(
                    "Cancelling pending deletion for {} (state: {:?})",
                    key, pending.state
                );
            }

            let user = self.extract_user_attributes(key, data);
            if self.is_feature_enabled() {
                let existing_users = self.get_existing_users();
                if existing_users.contains_key(key) {
                    self.update_user(key, &user)?;
                } else {
                    self.create_user(key, &user)?;
                }
            }
            self.users.insert(key.to_string(), user);
            Ok(None)
        } else {
            let next_timer = if self.is_feature_enabled() {
                let (_, timer) = self.delete_user(key)?;
                timer
            } else {
                None
            };
            self.users.remove(key);
            Ok(next_timer)
        }
    }

    /// Extract the security policy attributes
    fn extract_policy_attributes(&self, key: &str, data: &HashMap<String, String>) -> SecurityPolicy {
        let mut policy = SecurityPolicy {
            role: key.to_string(),
            ..Default::default()
        };

        for (field, value) in data {
            if field == "max_login_attempts" {
                policy.max_login_attempts = value.parse().unwrap_or(0);
            }
        }
        policy
    }

    /// Handle security policy table change from CONFIG_DB (LOCAL_ROLE_SECURITY_POLICY table)
    pub fn handle_policy_change(&mut self, key: &str, data: &HashMap<String, String>) -> UserdResult<Option<Instant>> {
        if !data.is_empty() {
            let policy = self.extract_policy_attributes(key, data);
            self.security_policies.insert(key.to_string(), policy);
        } else {
            self.security_policies.remove(key);
        }

        if self.is_feature_enabled() {
            self.update_security_policies()?;
        }

        Ok(None)
    }

    /// Handle metadata table change from CONFIG_DB (DEVICE_METADATA table)
    pub fn handle_metadata_change(&mut self, key: &str, data: &HashMap<String, String>) -> UserdResult<Option<Instant>> {
        if key != DEVICE_METADATA_LOCALHOST_KEY {
            return Ok(None);
        }

        let mut new_state = false;
        for (field, value) in data {
            if field == LOCAL_USER_MANAGEMENT_FIELD && value == "enabled" {
                new_state = true;
                break;
            }
        }

        if new_state != self.is_feature_enabled() || !self.initial_read_done {
            self.set_feature_enabled(new_state);
            self.handle_feature_config()?;
        }

        Ok(None)
    }

    /// Remove user from groups
    pub fn remove_user_from_groups(&self, username: &str, groups: &[&str]) -> UserdResult<()> {
        // Get user's current groups ONCE (avoid repeated 'groups' command calls)
        let current_groups: HashSet<String> = self.get_user_groups(username).into_iter().collect();

        for group in groups {
            if current_groups.contains(*group) {
                match run_gpasswd_remove(&self.cmd_executor, username, group)? {
                    GpasswdResult::Removed => {
                        info!("Removed user {} from group {}", username, group);
                    }
                    GpasswdResult::NotMember => {
                        debug!("User {} was not a member of group {}", username, group);
                    }
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ============================================================================
    // USERINFO STRUCTURE TESTS
    // ============================================================================

    #[test]
    fn test_user_info_equality() {
        let user1 = UserInfo {
            username: "testuser".to_string(),
            role: "administrator".to_string(),
            password_hash: "$6$salt$hash".to_string(),
            ssh_keys: vec!["ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ user@host".to_string()],
            enabled: true,
            uid: 1000,
            gid: 1000,
            home_dir: PathBuf::from("/home/testuser"),
            shell: "/bin/bash".to_string(),
        };

        let user2 = user1.clone();
        assert_eq!(user1, user2);

        // Change role
        let mut user3 = user1.clone();
        user3.role = "operator".to_string();
        assert_ne!(user1, user3);

        // Change enabled status
        let mut user4 = user1.clone();
        user4.enabled = false;
        user4.shell = NOLOGIN_SHELL.to_string();
        assert_ne!(user1, user4);
    }

    #[test]
    fn test_user_info_config_equals() {
        let user1 = UserInfo {
            username: "user1".to_string(),
            role: "operator".to_string(),
            password_hash: "hash".to_string(),
            ssh_keys: vec![],
            enabled: true,
            uid: 1000,
            gid: 1000,
            home_dir: PathBuf::from("/home/user1"),
            shell: "/bin/bash".to_string(),
        };

        // Same config but different uid/gid
        let user2 = UserInfo {
            username: "user2".to_string(),
            role: "operator".to_string(),
            password_hash: "hash".to_string(),
            ssh_keys: vec![],
            enabled: true,
            uid: 2000,
            gid: 2000,
            home_dir: PathBuf::from("/home/user2"),
            shell: "/bin/bash".to_string(),
        };

        // config_equals should compare configuration fields, not identity fields
        assert!(user1.config_equals(&user2));
    }

    // ============================================================================
    // CONSTANTS TESTS
    // ============================================================================

    #[test]
    fn test_role_group_mappings() {
        // Verify administrator role groups
        let admin_groups = ROLE_GROUPS
            .get("administrator")
            .expect("admin role should exist");
        assert_eq!(admin_groups.len(), 4);
        assert!(admin_groups.contains(&"sudo"));
        assert!(admin_groups.contains(&"docker"));
        assert!(admin_groups.contains(&"redis"));
        assert!(admin_groups.contains(&"admin"));

        // Verify operator role groups
        let operator_groups = ROLE_GROUPS
            .get("operator")
            .expect("operator role should exist");
        assert_eq!(operator_groups.len(), 1);
        assert!(operator_groups.contains(&"users"));
    }

    #[test]
    fn test_system_users_list() {
        // Verify some key system users are in the exclusion list
        assert!(SYSTEM_USERS.contains("root"));
        assert!(SYSTEM_USERS.contains("daemon"));
        assert!(SYSTEM_USERS.contains("www-data"));

        // Verify regular user names are not in the system users list
        assert!(!SYSTEM_USERS.contains("testuser"));
        assert!(!SYSTEM_USERS.contains("admin"));
        assert!(!SYSTEM_USERS.contains("operator"));
    }

    // ============================================================================
    // SSH KEY PARSING TESTS (Testing parsing logic)
    // ============================================================================

    #[test]
    fn test_ssh_key_parsing_json_array() {
        // Test JSON array parsing
        let json_str = r#"["ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ user1@host", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI user2@host"]"#;
        let keys: Vec<String> = serde_json::from_str(json_str).unwrap();
        assert_eq!(keys.len(), 2);
        assert_eq!(keys[0], "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ user1@host");
        assert_eq!(keys[1], "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI user2@host");
    }

    #[test]
    fn test_ssh_key_parsing_empty_array() {
        let json_str = "[]";
        let keys: Vec<String> = serde_json::from_str(json_str).unwrap();
        assert_eq!(keys.len(), 0);
    }

    #[test]
    fn test_ssh_key_parsing_comma_separated() {
        // Test comma-separated fallback
        let keys_str = "ssh-rsa KEY1 user1,ssh-ed25519 KEY2 user2";
        let keys: Vec<String> = keys_str
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        assert_eq!(keys.len(), 2);
        assert_eq!(keys[0], "ssh-rsa KEY1 user1");
        assert_eq!(keys[1], "ssh-ed25519 KEY2 user2");
    }

    // ============================================================================
    // USERMANAGER INTEGRATION TESTS WITH MOCKS
    // ============================================================================

    use crate::system::{CommandResult, MockCommandExecutor, MockSystemFunctions};
    use nix::unistd::{Gid, Group, Uid, User};
    use std::ffi::CString;
    use std::os::unix::process::ExitStatusExt;
    use std::path::PathBuf;
    use std::process::ExitStatus;

    /// Helper to create a successful CommandResult
    fn success_output() -> CommandResult {
        CommandResult {
            status: ExitStatus::from_raw(0),
            stdout: Vec::new(),
            stderr: Vec::new(),
            command_str: "test command".to_string(),
        }
    }

    /// Helper to create a failed CommandResult
    fn failure_output(stderr: &str) -> CommandResult {
        CommandResult {
            status: ExitStatus::from_raw(256), // exit code 1
            stdout: Vec::new(),
            stderr: stderr.as_bytes().to_vec(),
            command_str: "test command".to_string(),
        }
    }

    /// Helper to create a CommandResult with custom output
    fn mock_result(status: ExitStatus, stdout: Vec<u8>, stderr: Vec<u8>) -> CommandResult {
        CommandResult {
            status,
            stdout,
            stderr,
            command_str: "test command".to_string(),
        }
    }

    /// Helper to create a test UserInfo
    fn test_user_info(username: &str, role: &str, enabled: bool) -> UserInfo {
        UserInfo {
            username: username.to_string(),
            role: role.to_string(),
            password_hash: "$6$salt$testhash".to_string(),
            ssh_keys: vec![],
            enabled,
            uid: 1001,
            gid: 1001,
            home_dir: PathBuf::from(format!("/home/{}", username)),
            shell: if enabled {
                DEFAULT_SHELL.to_string()
            } else {
                NOLOGIN_SHELL.to_string()
            },
        }
    }

    // ============================================================================
    // CREATE USER TESTS
    // ============================================================================

    #[test]
    fn test_create_user_already_exists() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User already exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // No commands should be executed
        mock_cmd.expect_execute().times(0);

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let user_config = test_user_info("testuser", "operator", true);
        let result = manager.create_user("testuser", &user_config);

        assert!(result.is_ok());
        assert!(!result.unwrap()); // Returns false when user already exists
    }

    #[test]
    fn test_create_user_invalid_role() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User doesn't exist
        mock_sys.expect_getpwnam().returning(|_| None);

        // No commands should be executed for invalid role
        mock_cmd.expect_execute().times(0);

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let mut user_config = test_user_info("testuser", "invalid_role", true);
        user_config.role = "invalid_role".to_string();
        let result = manager.create_user("testuser", &user_config);

        assert!(result.is_err());
        match result.unwrap_err() {
            UserdError::InvalidRole(role) => assert_eq!(role, "invalid_role"),
            _ => panic!("Expected InvalidRole error"),
        }
    }

    // ============================================================================
    // DELETE USER TESTS
    // ============================================================================

    #[test]
    fn test_delete_user_not_exists() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User doesn't exist
        mock_sys.expect_getpwnam().returning(|_| None);

        // No commands should be executed
        mock_cmd.expect_execute().times(0);

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("nonexistent");

        assert!(result.is_ok());
        let (deleted, timer) = result.unwrap();
        assert!(!deleted); // Returns false when user doesn't exist
        assert!(timer.is_none()); // No timer when user doesn't exist
    }

    #[test]
    fn test_delete_user_success() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // Expect userdel command
        mock_cmd
            .expect_execute()
            .withf(|cmd| {
                cmd.len() == 3
                    && cmd[0] == CMD_USERDEL
                    && cmd[1] == "-r"
                    && cmd[2] == "testuser"
            })
            .times(1)
            .returning(|_| Ok(success_output()));

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("testuser");

        assert!(result.is_ok());
        let (deleted, timer) = result.unwrap();
        assert!(deleted); // Returns true on successful deletion
        assert!(timer.is_none()); // No timer when deletion succeeds immediately
    }

    #[test]
    fn test_delete_user_ignores_mail_spool_error() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel returns error about mail spool (benign)
        mock_cmd
            .expect_execute()
            .returning(|_| Ok(failure_output("userdel: user testuser mail spool not found")));

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("testuser");

        // Should succeed despite mail spool error
        assert!(result.is_ok());
        let (deleted, timer) = result.unwrap();
        assert!(deleted);
        assert!(timer.is_none());
    }

    #[test]
    fn test_delete_user_fails_on_mail_spool_plus_other_errors() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel returns mail spool error AND another error (should fail)
        mock_cmd.expect_execute().returning(|_| {
            Ok(failure_output(
                "userdel: cannot remove mail spool /var/mail/testuser\nuserdel: error removing home directory /home/testuser: Permission denied"
            ))
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("testuser");

        // Should fail because there's a real error in addition to mail spool
        assert!(result.is_err());
    }

    // ============================================================================
    // SET USER PASSWORD TESTS
    // ============================================================================

    #[test]
    fn test_set_user_password_success() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Expect usermod -p command with masked password
        mock_cmd
            .expect_execute_masked()
            .withf(|cmd, mask_indices| {
                cmd.len() == 4
                    && cmd[0] == CMD_USERMOD
                    && cmd[1] == "-p"
                    && cmd[2] == "$6$salt$hash"
                    && cmd[3] == "testuser"
                    && mask_indices.contains(&2) // Password should be masked
            })
            .times(1)
            .returning(|_, _| Ok(success_output()));

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.set_user_password("testuser", "$6$salt$hash");
        assert!(result.is_ok());
    }

    #[test]
    fn test_set_user_password_failure() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // usermod fails
        mock_cmd
            .expect_execute_masked()
            .returning(|_, _| Ok(failure_output("usermod: user 'testuser' does not exist")));

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.set_user_password("testuser", "$6$salt$hash");
        assert!(result.is_err());
    }

    // ============================================================================
    // SET USER SHELL TESTS
    // ============================================================================

    #[test]
    fn test_set_user_shell_enabled() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Expect usermod -s /bin/bash
        mock_cmd
            .expect_execute()
            .withf(|cmd| {
                cmd.len() == 4
                    && cmd[0] == CMD_USERMOD
                    && cmd[1] == "-s"
                    && cmd[2] == DEFAULT_SHELL
                    && cmd[3] == "testuser"
            })
            .times(1)
            .returning(|_| Ok(success_output()));

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.set_user_shell("testuser", true);
        assert!(result.is_ok());
    }

    #[test]
    fn test_set_user_shell_disabled() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Expect usermod -s NOLOGIN_SHELL
        mock_cmd
            .expect_execute()
            .withf(|cmd| {
                cmd.len() == 4
                    && cmd[0] == CMD_USERMOD
                    && cmd[1] == "-s"
                    && cmd[2] == NOLOGIN_SHELL
                    && cmd[3] == "testuser"
            })
            .times(1)
            .returning(|_| Ok(success_output()));

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.set_user_shell("testuser", false);
        assert!(result.is_ok());
    }

    // ============================================================================
    // IS USER MANAGED TESTS
    // ============================================================================

    #[test]
    fn test_is_user_managed_true() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Simulate `groups username` returning groups including local_mgd
        // Format: "username : group1 group2 group3"
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                "testuser : users docker local_mgd\n".as_bytes().to_vec(),
                Vec::new(),
            ))
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        assert!(manager.is_user_managed("testuser"));
    }

    #[test]
    fn test_is_user_managed_false() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Simulate `groups username` returning groups without local_mgd
        // Format: "username : group1 group2 group3"
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                "testuser : users docker\n".as_bytes().to_vec(),
                Vec::new(),
            ))
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        assert!(!manager.is_user_managed("testuser"));
    }

    // ============================================================================
    // UNMANAGE USER TESTS
    // ============================================================================

    #[test]
    fn test_unmanage_user_success() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // First call: groups to check if managed (returns groups with local_mgd)
        // Second call: gpasswd -d to remove from managed group
        let call_count = std::cell::RefCell::new(0);
        mock_cmd.expect_execute().returning(move |cmd| {
            let mut count = call_count.borrow_mut();
            *count += 1;
            if *count == 1 {
                // First call: groups - format: "username : group1 group2"
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    "testuser : local_mgd users\n".as_bytes().to_vec(),
                    Vec::new(),
                ))
            } else {
                // Second call: gpasswd -d
                assert_eq!(cmd[0], CMD_GPASSWD);
                assert_eq!(cmd[1], "-d");
                assert_eq!(cmd[2], "testuser");
                assert_eq!(cmd[3], "local_mgd");
                Ok(success_output())
            }
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.unmanage_user("testuser");
        assert!(result.is_ok());
        assert!(result.unwrap());
    }

    #[test]
    fn test_unmanage_user_not_managed() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // User is not in managed group - groups command format: "username : group1 group2"
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                "testuser : users docker\n".as_bytes().to_vec(),
                Vec::new(),
            ))
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.unmanage_user("testuser");
        assert!(result.is_ok());
        assert!(!result.unwrap()); // Returns false - user wasn't managed
    }

    // ============================================================================
    // FEATURE ENABLED TESTS
    // ============================================================================

    #[test]
    fn test_feature_enabled_default_false() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let manager = UserManager::new(mock_cmd, mock_sys);

        assert!(!manager.is_feature_enabled());
    }

    #[test]
    fn test_set_feature_enabled() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        manager.set_feature_enabled(true);
        assert!(manager.is_feature_enabled());

        manager.set_feature_enabled(false);
        assert!(!manager.is_feature_enabled());
    }

    // ============================================================================
    // ENSURE MANAGED GROUP EXISTS TESTS
    // ============================================================================

    #[test]
    fn test_ensure_managed_group_exists_already_exists() {
        let mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // Group already exists
        mock_sys.expect_getgrnam().returning(|name| {
            Some(Group {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                gid: Gid::from_raw(1000),
                mem: vec![],
            })
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.ensure_managed_group_exists();
        assert!(result.is_ok());
        assert!(result.unwrap());
    }

    #[test]
    fn test_ensure_managed_group_exists_creates_group() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // Group doesn't exist
        mock_sys.expect_getgrnam().returning(|_| None);

        // Expect groupadd command
        mock_cmd
            .expect_execute()
            .withf(|cmd| cmd.len() == 2 && cmd[0] == CMD_GROUPADD && cmd[1] == "local_mgd")
            .times(1)
            .returning(|_| Ok(success_output()));

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.ensure_managed_group_exists();
        assert!(result.is_ok());
        assert!(result.unwrap());
    }

    // ============================================================================
    // REMOVE USER FROM GROUPS TESTS
    // ============================================================================

    #[test]
    fn test_remove_user_from_groups() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        mock_cmd.expect_execute().returning(move |cmd| {
            // groups command is used to check membership
            // Format: "username : group1 group2 group3"
            if cmd[0] == CMD_GROUPS {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    "testuser : sudo docker admin\n".as_bytes().to_vec(),
                    Vec::new(),
                ))
            } else if cmd[0] == CMD_GPASSWD {
                // gpasswd -d to remove from group
                Ok(success_output())
            } else {
                panic!("Unexpected command: {:?}", cmd);
            }
        });

        let manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.remove_user_from_groups("testuser", &["sudo", "docker"]);
        assert!(result.is_ok());
    }

    // ============================================================================
    // HANDLE USER CHANGE TESTS
    // ============================================================================

    #[test]
    fn test_handle_user_change_feature_disabled() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // No commands should be executed when feature is disabled
        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(false);

        let mut data = HashMap::new();
        data.insert("role".to_string(), "administrator".to_string());
        data.insert("enabled".to_string(), "true".to_string());

        let result = manager.handle_user_change("testuser", &data);
        assert!(result.is_ok());
        // User SHOULD be cached even when feature is disabled (no system sync, but cached)
        assert!(!manager.users.is_empty());
        assert!(manager.users.contains_key("testuser"));
    }

    #[test]
    fn test_handle_user_change_delete_when_disabled() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Delete (empty data) should also be ignored when feature is disabled
        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(false);

        let data = HashMap::new(); // Empty data = delete
        let result = manager.handle_user_change("testuser", &data);
        assert!(result.is_ok());
    }

    // ============================================================================
    // HANDLE POLICY CHANGE TESTS
    // ============================================================================

    #[test]
    fn test_handle_policy_change_feature_disabled() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // No commands should be executed when feature is disabled
        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(false);

        let mut data = HashMap::new();
        data.insert("max_login_attempts".to_string(), "5".to_string());

        let result = manager.handle_policy_change("administrator", &data);
        assert!(result.is_ok());
        // Policy SHOULD be cached even when feature is disabled (no system sync, but cached)
        assert!(!manager.security_policies.is_empty());
        assert!(manager.security_policies.contains_key("administrator"));
    }

    #[test]
    fn test_handle_policy_change_delete_when_disabled() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // Delete (empty data) should also be ignored when feature is disabled
        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(false);

        let data = HashMap::new(); // Empty data = delete
        let result = manager.handle_policy_change("administrator", &data);
        assert!(result.is_ok());
    }

    // ============================================================================
    // HANDLE METADATA CHANGE TESTS
    // ============================================================================

    #[test]
    fn test_handle_metadata_change_ignores_non_localhost() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Non-localhost keys should be ignored
        let mut data = HashMap::new();
        data.insert("local_user_management".to_string(), "enabled".to_string());

        let result = manager.handle_metadata_change("not_localhost", &data);
        assert!(result.is_ok());
        // Feature should remain disabled (default)
        assert!(!manager.is_feature_enabled());
    }

    #[test]
    fn test_handle_metadata_change_already_enabled() {
        // Test that when feature is already enabled and initial read is done,
        // no state change occurs and no side effects are triggered.
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(true); // Already enabled
        manager.set_initial_read_done(); // Mark initial read as done

        let mut data = HashMap::new();
        data.insert("local_user_management".to_string(), "enabled".to_string());

        // Should succeed because no state change needed (enabled → enabled, initial read done)
        let result = manager.handle_metadata_change("localhost", &data);
        assert!(result.is_ok());
        assert!(manager.is_feature_enabled());
    }

    #[test]
    fn test_handle_metadata_change_parses_enabled_value() {
        // Test that the "enabled" value is correctly parsed from metadata
        // We test this indirectly through the non-localhost ignore test
        // and the disable test. This test verifies only "enabled" value triggers enable.
        let mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // cleanup_managed_state() calls getgrnam to discover managed users
        mock_sys.expect_getgrnam().returning(|name| {
            if name == MANAGED_USER_GROUP {
                Some(Group {
                    name: name.to_string(),
                    passwd: CString::new("x").unwrap(),
                    gid: Gid::from_raw(1000),
                    mem: vec![], // No users to cleanup
                })
            } else {
                None
            }
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(true);

        // "disabled" value should trigger transition (but won't fail because
        // cleanup_managed_state gracefully handles j2 errors with warn!)
        let mut data = HashMap::new();
        data.insert("local_user_management".to_string(), "disabled".to_string());

        let result = manager.handle_metadata_change("localhost", &data);
        assert!(result.is_ok());
        assert!(!manager.is_feature_enabled()); // Should be disabled now

        // Any other value (not "enabled") should also be treated as disabled
        manager.set_feature_enabled(true);
        data.insert("local_user_management".to_string(), "invalid".to_string());
        let result = manager.handle_metadata_change("localhost", &data);
        assert!(result.is_ok());
        assert!(!manager.is_feature_enabled());
    }

    #[test]
    fn test_handle_metadata_change_disable_triggers_cleanup() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // First enable the feature
        mock_sys.expect_getgrnam().returning(|name| {
            if name == MANAGED_USER_GROUP {
                // Return group with one member for cleanup
                Some(Group {
                    name: name.to_string(),
                    passwd: CString::new("x").unwrap(),
                    gid: Gid::from_raw(1000),
                    mem: vec!["testuser".to_string()],
                })
            } else {
                None
            }
        });

        // Mock commands for cleanup (groups check, gpasswd)
        mock_cmd.expect_execute().returning(|cmd| {
            if cmd[0] == CMD_GROUPS {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    "testuser : local_mgd users\n".as_bytes().to_vec(),
                    Vec::new(),
                ))
            } else if cmd[0] == CMD_GPASSWD {
                Ok(success_output())
            } else {
                Ok(success_output())
            }
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(true);

        // Add a user and policy to internal state
        manager.users.insert(
            "testuser".to_string(),
            test_user_info("testuser", "operator", true),
        );
        manager.security_policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 5,
            },
        );

        // Now disable
        let mut data = HashMap::new();
        data.insert("local_user_management".to_string(), "disabled".to_string());

        let result = manager.handle_metadata_change("localhost", &data);
        assert!(result.is_ok());
        assert!(!manager.is_feature_enabled());
        // Internal state should be cleared
        assert!(manager.users.is_empty());
        assert!(manager.security_policies.is_empty());
    }

    // ============================================================================
    // CLEANUP MANAGED STATE TESTS
    // ============================================================================

    #[test]
    fn test_cleanup_managed_state_no_group() {
        let mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // Managed group doesn't exist
        mock_sys
            .expect_getgrnam()
            .returning(|_| None);

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add some internal state
        manager.users.insert(
            "testuser".to_string(),
            test_user_info("testuser", "operator", true),
        );
        manager.security_policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 5,
            },
        );

        manager.cleanup_managed_state();

        // Internal state should be cleared even if group doesn't exist
        assert!(manager.users.is_empty());
        assert!(manager.security_policies.is_empty());
    }

    #[test]
    fn test_cleanup_managed_state_with_users() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // Managed group exists with users
        mock_sys.expect_getgrnam().returning(|name| {
            if name == MANAGED_USER_GROUP {
                Some(Group {
                    name: name.to_string(),
                    passwd: CString::new("x").unwrap(),
                    gid: Gid::from_raw(1000),
                    mem: vec!["user1".to_string(), "user2".to_string()],
                })
            } else {
                None
            }
        });

        // Mock commands for unmanage_user calls
        // Each unmanage_user calls: groups (check), gpasswd -d (remove)
        mock_cmd.expect_execute().returning(|cmd| {
            if cmd[0] == CMD_GROUPS {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    "user1 : local_mgd users\n".as_bytes().to_vec(),
                    Vec::new(),
                ))
            } else if cmd[0] == CMD_GPASSWD {
                Ok(success_output())
            } else {
                Ok(success_output())
            }
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add internal state
        manager.users.insert(
            "user1".to_string(),
            test_user_info("user1", "operator", true),
        );
        manager.security_policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 3,
            },
        );

        manager.cleanup_managed_state();

        // Internal state should be cleared
        assert!(manager.users.is_empty());
        assert!(manager.security_policies.is_empty());
    }

    #[test]
    fn test_cleanup_managed_state_empty_group() {
        let mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // Managed group exists but has no members
        mock_sys.expect_getgrnam().returning(|name| {
            Some(Group {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                gid: Gid::from_raw(1000),
                mem: vec![],
            })
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add internal state
        manager.security_policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 5,
            },
        );

        manager.cleanup_managed_state();

        // Internal state should be cleared
        assert!(manager.users.is_empty());
        assert!(manager.security_policies.is_empty());
    }

    // ============================================================================
    // PENDING DELETION TESTS
    // ============================================================================

    #[test]
    fn test_delete_user_with_active_processes_adds_to_pending_queue() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel fails with "currently used by process" error
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"userdel: user testuser is currently used by process 1234".to_vec(),
            ))
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("testuser");

        // Should succeed (returns Ok(true) even though deletion is pending)
        assert!(result.is_ok());
        let (deleted, timer) = result.unwrap();
        assert!(deleted);
        assert!(timer.is_some()); // Timer should be set for pending deletion

        // User should be in pending deletion queue
        assert_eq!(manager.pending_deletions.len(), 1);
        assert!(manager.pending_deletions.contains_key("testuser"));

        let pending = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending.username, "testuser");
        assert_eq!(pending.state, crate::types::DeletionState::Initial);
        assert_eq!(pending.retry_count, 0);
    }

    #[test]
    fn test_delete_user_other_errors_not_added_to_pending() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel fails with different error (not "currently used by process")
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"userdel: some other error".to_vec(),
            ))
        });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        let result = manager.delete_user("testuser");

        // Should fail with error
        assert!(result.is_err());

        // User should NOT be in pending deletion queue
        assert_eq!(manager.pending_deletions.len(), 0);
    }

    #[test]
    fn test_handle_user_change_cancels_pending_deletion() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let mut manager = UserManager::new(mock_cmd, mock_sys);
        manager.set_feature_enabled(false); // Disable feature to avoid actual system calls

        // Manually add a user to pending deletion queue
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::TermSent,
            next_action_time: Instant::now() + Duration::from_secs(5),
            retry_count: 1,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        assert_eq!(manager.pending_deletions.len(), 1);

        // Re-add user to CONFIG_DB
        let mut data = HashMap::new();
        data.insert("role".to_string(), "administrator".to_string());
        data.insert("enabled".to_string(), "true".to_string());

        let result = manager.handle_user_change("testuser", &data);
        assert!(result.is_ok());

        // Pending deletion should be cancelled
        assert_eq!(manager.pending_deletions.len(), 0);
        assert!(!manager.pending_deletions.contains_key("testuser"));
    }

    #[test]
    fn test_process_pending_deletions_initial_to_term_sent() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // pkill should be called with SIGTERM
        mock_cmd
            .expect_execute()
            .withf(|cmd| {
                cmd.len() >= 4
                    && cmd[0].contains("pkill")
                    && cmd[1] == "-TERM"
                    && cmd[2] == "-u"
                    && cmd[3] == "testuser"
            })
            .times(1)
            .returning(|_| {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    Vec::new(),
                    Vec::new(),
                ))
            });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in Initial state with action time in the past
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::Initial,
            next_action_time: Instant::now() - Duration::from_secs(1), // In the past
            retry_count: 0,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // State should transition to TermSent
        let pending = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending.state, DeletionState::TermSent);
        assert_eq!(pending.retry_count, 0);
    }

    #[test]
    fn test_process_pending_deletions_term_sent_to_kill_sent() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        // pkill should be called with SIGKILL
        mock_cmd
            .expect_execute()
            .withf(|cmd| {
                cmd.len() >= 4
                    && cmd[0].contains("pkill")
                    && cmd[1] == "-KILL"
                    && cmd[2] == "-u"
                    && cmd[3] == "testuser"
            })
            .times(1)
            .returning(|_| {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    Vec::new(),
                    Vec::new(),
                ))
            });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in TermSent state with action time in the past
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::TermSent,
            next_action_time: Instant::now() - Duration::from_secs(1), // In the past
            retry_count: 0,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // State should transition to KillSent
        let pending = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending.state, DeletionState::KillSent);
        assert_eq!(pending.retry_count, 0);
    }

    #[test]
    fn test_process_pending_deletions_kill_sent_success() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel should succeed
        mock_cmd
            .expect_execute()
            .withf(|cmd| cmd.len() >= 3 && cmd[0].contains("userdel") && cmd[2] == "testuser")
            .times(1)
            .returning(|_| {
                Ok(mock_result(
                    ExitStatus::from_raw(0),
                    Vec::new(),
                    Vec::new(),
                ))
            });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in KillSent state with action time in the past
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::KillSent,
            next_action_time: Instant::now() - Duration::from_secs(1), // In the past
            retry_count: 0,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // User should be removed from pending queue (successfully deleted)
        assert_eq!(manager.pending_deletions.len(), 0);
        assert!(!manager.pending_deletions.contains_key("testuser"));
    }

    #[test]
    fn test_process_pending_deletions_kill_sent_retry_on_failure() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel fails (still has processes)
        mock_cmd
            .expect_execute()
            .withf(|cmd| cmd.len() >= 3 && cmd[0].contains("userdel") && cmd[2] == "testuser")
            .times(1)
            .returning(|_| {
                Ok(mock_result(
                    ExitStatus::from_raw(256), // Exit code 1
                    Vec::new(),
                    b"userdel: user testuser is currently used by process 1234".to_vec(),
                ))
            });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in KillSent state with action time in the past
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::KillSent,
            next_action_time: Instant::now() - Duration::from_secs(1), // In the past
            retry_count: 0,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // User should still be in pending queue, state reset to Initial, retry_count incremented
        assert_eq!(manager.pending_deletions.len(), 1);
        let pending = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending.state, DeletionState::Initial);
        assert_eq!(pending.retry_count, 1);
    }

    #[test]
    fn test_process_pending_deletions_max_retries_reached() {
        let mut mock_cmd = MockCommandExecutor::new();
        let mut mock_sys = MockSystemFunctions::new();

        // User exists
        mock_sys.expect_getpwnam().returning(|name| {
            Some(User {
                name: name.to_string(),
                passwd: CString::new("x").unwrap(),
                uid: Uid::from_raw(1001),
                gid: Gid::from_raw(1001),
                gecos: CString::new("").unwrap(),
                dir: PathBuf::from(format!("/home/{}", name)),
                shell: PathBuf::from("/bin/bash"),
            })
        });

        // userdel fails (still has processes)
        mock_cmd
            .expect_execute()
            .withf(|cmd| cmd.len() >= 3 && cmd[0].contains("userdel") && cmd[2] == "testuser")
            .times(1)
            .returning(|_| {
                Ok(mock_result(
                    ExitStatus::from_raw(256), // Exit code 1
                    Vec::new(),
                    b"userdel: user testuser is currently used by process 1234".to_vec(),
                ))
            });

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in KillSent state with retry_count = 2 (next failure will be 3rd retry)
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::KillSent,
            next_action_time: Instant::now() - Duration::from_secs(1), // In the past
            retry_count: 2,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending);

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // User should still be in pending queue but marked as Failed
        assert_eq!(manager.pending_deletions.len(), 1);
        let pending = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending.state, DeletionState::Failed);
        assert_eq!(pending.retry_count, 3);
    }

    #[test]
    fn test_process_pending_deletions_skips_future_actions() {
        let mock_cmd = MockCommandExecutor::new();
        let mock_sys = MockSystemFunctions::new();

        let mut manager = UserManager::new(mock_cmd, mock_sys);

        // Add user in Initial state with action time in the future
        use crate::types::{DeletionState, PendingDeletion};
        use std::time::{Duration, Instant};

        let pending = PendingDeletion {
            username: "testuser".to_string(),
            state: DeletionState::Initial,
            next_action_time: Instant::now() + Duration::from_secs(60), // In the future
            retry_count: 0,
        };
        manager.pending_deletions.insert("testuser".to_string(), pending.clone());

        // Process pending deletions
        let _next_check = manager.process_pending_deletions();

        // State should NOT change (action time is in the future)
        let pending_after = manager.pending_deletions.get("testuser").unwrap();
        assert_eq!(pending_after.state, DeletionState::Initial);
        assert_eq!(pending_after.retry_count, 0);
    }
}
