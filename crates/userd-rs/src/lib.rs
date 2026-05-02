//! SONiC User Management Daemon (userd) in Rust
//!
//! This crate provides a daemon for managing local users on SONiC devices.
//! It monitors CONFIG_DB for user configuration changes and synchronizes
//! the system state accordingly.
//!
//! # Features
//!
//! - User creation, modification, and deletion
//! - Role-based group assignment (administrator, operator)
//! - SSH key management
//! - PAM faillock security policies
//! - Consistency checking at startup
//!
//! # Architecture
//!
//! The daemon uses trait-based dependency injection for testability:
//! - `CommandExecutorTrait`: For executing system commands
//! - `SystemFunctionsTrait`: For system calls (getpwnam, chown, etc.)
//! - `commands`: Wrapper functions for system commands (useradd, userdel, etc.)

pub mod commands;
pub mod error;
pub mod system;
pub mod templates;
pub mod types;
pub mod user_manager;

pub use commands::{
    run_gpasswd_remove, run_groupadd, run_groups, run_pkill, run_useradd, run_userdel,
    run_usermod_add_group, run_usermod_password, run_usermod_shell, GpasswdResult,
};
pub use error::{UserdError, UserdResult};
pub use system::{
    CommandExecutor, CommandExecutorTrait, CommandResult, SystemFunctions, SystemFunctionsTrait,
};

// Re-export nix types that are part of our public API
pub use nix::unistd::{Gid, Group, Uid, User};
pub use templates::render_pam_faillock;
pub use types::{SecurityPolicy, UserInfo};
pub use user_manager::UserManager;
