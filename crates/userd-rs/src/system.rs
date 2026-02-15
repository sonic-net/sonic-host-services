//! System abstraction layer for dependency injection
//!
//! Provides traits for system operations to enable testing

use std::collections::HashSet;
use std::path::Path;
use std::process::{Command, ExitStatus};

use nix::unistd::{Gid, Group, Uid, User};
use tracing::{debug, error};

use crate::error::{UserdError, UserdResult};

#[cfg(test)]
use mockall::automock;

/// Result of executing a command, including the output and the command string
pub struct CommandResult {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub command_str: String,
}

impl CommandResult {
    /// Check if the command succeeded (exit code 0)
    pub fn success(&self) -> bool {
        self.status.success()
    }

    /// Get the exit code of the command
    pub fn code(&self) -> Option<i32> {
        self.status.code()
    }

    /// Get stderr as a UTF-8 string (lossy conversion)
    pub fn stderr_str(&self) -> String {
        String::from_utf8_lossy(&self.stderr).to_string()
    }

    /// Get stdout as a UTF-8 string (lossy conversion)
    pub fn stdout_str(&self) -> String {
        String::from_utf8_lossy(&self.stdout).to_string()
    }

    /// Convert a failed command result to a CommandError
    pub fn to_error(self) -> UserdError {
        let message = String::from_utf8_lossy(&self.stderr).to_string();
        UserdError::CommandError {
            command: self.command_str,
            message,
        }
    }
}

/// Trait for executing system commands
#[cfg_attr(test, automock(type MockCommandExecutor;))]
pub trait CommandExecutorTrait: Send + Sync {
    /// Execute a command and return the result with command string
    fn execute(&self, cmd: Vec<String>) -> UserdResult<CommandResult>;

    /// Execute a command with masked arguments (for logging sensitive data)
    fn execute_masked(&self, cmd: Vec<String>, mask_indices: HashSet<usize>) -> UserdResult<CommandResult>;
}

/// Trait for system functions (file operations, user lookups)
#[cfg_attr(test, automock(type MockSystemFunctions;))]
pub trait SystemFunctionsTrait: Send + Sync {
    /// Get password entry for a user by name
    fn getpwnam(&self, name: &str) -> Option<User>;

    /// Get group entry for a group by name
    fn getgrnam(&self, name: &str) -> Option<Group>;

    /// Change file ownership
    fn chown(&self, path: &Path, uid: Uid, gid: Gid) -> UserdResult<()>;

    /// Change file permissions
    fn chmod(&self, path: &Path, mode: u32) -> UserdResult<()>;

    /// Check file access
    fn access(&self, path: &Path, mode: i32) -> bool;

    /// Remove a file
    fn unlink(&self, path: &Path) -> UserdResult<()>;
}

pub struct CommandExecutor;

impl CommandExecutorTrait for CommandExecutor {
    fn execute(&self, cmd: Vec<String>) -> UserdResult<CommandResult> {
        self.execute_masked(cmd, HashSet::new())
    }

    fn execute_masked(&self, cmd: Vec<String>, mask_indices: HashSet<usize>) -> UserdResult<CommandResult> {
        if cmd.is_empty() {
            return Err(UserdError::CommandError {
                command: String::new(),
                message: "Empty command".to_string(),
            });
        }

        // Build command string for logging with sensitive arguments masked
        let command_str: String = cmd
            .iter()
            .enumerate()
            .map(|(i, arg)| {
                if mask_indices.contains(&i) {
                    "***".to_string()
                } else {
                    arg.clone()
                }
            })
            .collect::<Vec<_>>()
            .join(" ");

        debug!("Executing command: {}", command_str);

        let output = Command::new(&cmd[0]).args(&cmd[1..]).output().map_err(|e| {
            UserdError::CommandError {
                command: command_str.clone(),
                message: e.to_string(),
            }
        })?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            error!(
                "Command failed with status {:?}: {} - {}",
                output.status.code(),
                command_str,
                stderr
            );
        }

        Ok(CommandResult {
            status: output.status,
            stdout: output.stdout,
            stderr: output.stderr,
            command_str,
        })
    }
}

pub struct SystemFunctions;

impl SystemFunctionsTrait for SystemFunctions {
    fn getpwnam(&self, name: &str) -> Option<User> {
        match nix::unistd::User::from_name(name) {
            Ok(user) => user,
            Err(e) => {
                error!("getpwnam failed for '{}': {}", name, e);
                None
            }
        }
    }

    fn getgrnam(&self, name: &str) -> Option<Group> {
        match nix::unistd::Group::from_name(name) {
            Ok(group) => group,
            Err(e) => {
                error!("getgrnam failed for '{}': {}", name, e);
                None
            }
        }
    }

    fn chown(&self, path: &Path, uid: Uid, gid: Gid) -> UserdResult<()> {
        nix::unistd::chown(path, Some(uid), Some(gid))?;
        Ok(())
    }

    fn chmod(&self, path: &Path, mode: u32) -> UserdResult<()> {
        use nix::sys::stat::Mode;
        let mode = Mode::from_bits_truncate(mode);
        nix::sys::stat::fchmodat(
            None,
            path,
            mode,
            nix::sys::stat::FchmodatFlags::FollowSymlink,
        )?;
        Ok(())
    }

    fn access(&self, path: &Path, mode: i32) -> bool {
        use nix::unistd::AccessFlags;
        let flags = AccessFlags::from_bits_truncate(mode);
        nix::unistd::access(path, flags).is_ok()
    }

    fn unlink(&self, path: &Path) -> UserdResult<()> {
        std::fs::remove_file(path)?;
        Ok(())
    }
}
