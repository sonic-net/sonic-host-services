//! System command wrappers
//!
//! This module provides wrapper functions for system commands used in user management.
//! Following the pattern from sonic-swss/cfgmgr/shellcmd.h for command path definitions,
//! these wrappers ensure consistent error handling and logging across the codebase.

use tracing::{debug, error, info};

use crate::error::UserdResult;
use crate::system::CommandExecutorTrait;
use crate::types::*;

/// Helper macro to convert string literals to Vec<String> for command execution
#[macro_export]
macro_rules! cmd {
    ($($arg:expr),+ $(,)?) => {
        vec![$($arg.to_string()),+]
    };
}

/// Result type for gpasswd operations that may partially succeed
#[derive(Debug, PartialEq)]
pub enum GpasswdResult {
    /// User was successfully removed from the group
    Removed,
    /// User was not a member of the group (not an error)
    NotMember,
}

/// Run `groups` command to get the groups a user belongs to
pub fn run_groups<C: CommandExecutorTrait>(cmd_executor: &C, username: &str) -> Vec<String> {
    let output = cmd_executor.execute(cmd![CMD_GROUPS, username]);
    match output {
        Ok(result) if result.success() => {
            let stdout = result.stdout_str();
            // Format: "username : group1 group2 group3"
            if let Some(pos) = stdout.find(':') {
                return stdout[pos + 1..]
                    .split_whitespace()
                    .map(|s| s.to_string())
                    .collect();
            }
            Vec::new()
        }
        _ => Vec::new(),
    }
}

/// Run `gpasswd -d` to remove a user from a group
pub fn run_gpasswd_remove<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    group: &str,
) -> UserdResult<GpasswdResult> {
    let result = cmd_executor.execute(cmd![CMD_GPASSWD, "-d", username, group])?;
    if result.success() {
        debug!("Removed user {} from group {}", username, group);
        Ok(GpasswdResult::Removed)
    } else {
        let stderr = result.stderr_str();
        // "not a member" is not an error - user wasn't in the group
        if stderr.contains("not a member") {
            debug!("User {} was not a member of group {}", username, group);
            Ok(GpasswdResult::NotMember)
        } else {
            Err(result.to_error())
        }
    }
}

/// Run `groupadd` to create a new system group
pub fn run_groupadd<C: CommandExecutorTrait>(cmd_executor: &C, group: &str) -> UserdResult<bool> {
    let result = cmd_executor.execute(cmd![CMD_GROUPADD, group])?;
    if result.success() {
        info!("Created group {}", group);
        Ok(true)
    } else {
        let stderr = result.stderr_str();
        // Group already exists is not necessarily an error
        if stderr.contains("already exists") {
            debug!("Group {} already exists", group);
            Ok(true)
        } else {
            error!("Failed to create group {}: {}", group, stderr);
            Ok(false)
        }
    }
}

/// Run `useradd` to create a new user
pub fn run_useradd<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    home_dir: &str,
    shell: &str,
) -> UserdResult<()> {
    let result = cmd_executor.execute(cmd![
        CMD_USERADD,
        "-d", home_dir,
        "-m",
        "-s", shell,
        username
    ])?;
    if result.success() {
        debug!("Created user {} with home {} and shell {}", username, home_dir, shell);
        Ok(())
    } else {
        Err(result.to_error())
    }
}

/// Run `userdel -r` to delete a user and their home directory
pub fn run_userdel<C: CommandExecutorTrait>(cmd_executor: &C, username: &str) -> UserdResult<()> {
    let result = cmd_executor.execute(cmd![CMD_USERDEL, "-r", username])?;
    if result.success() {
        debug!("Deleted user {}", username);
        Ok(())
    } else {
        let stderr = result.stderr_str();
        // Check if the ONLY error is a benign mail spool warning
        // userdel outputs "mail spool (/path) not found" when there's no mail spool (not an error)
        // We need to ensure this is the ONLY error - if there are other errors, we should fail
        let is_only_mail_spool_error = stderr.contains("mail spool")
            && stderr.contains("not found")
            && !stderr.lines().any(|line| {
                let line = line.trim();
                !line.is_empty()
                && !line.contains("mail spool")
                && line.starts_with("userdel:")
            });

        if is_only_mail_spool_error {
            debug!("Deleted user {} (mail spool warning ignored)", username);
            Ok(())
        } else {
            Err(result.to_error())
        }
    }
}

/// Run `usermod -p` to set a user's password hash
pub fn run_usermod_password<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    password_hash: &str,
) -> UserdResult<()> {
    // Index 2 is the password_hash argument which should be masked in logs
    let result = cmd_executor.execute_masked(
        cmd![CMD_USERMOD, "-p", password_hash, username],
        [2].into_iter().collect(),
    )?;
    if result.success() {
        debug!("Set password for user {}", username);
        Ok(())
    } else {
        Err(result.to_error())
    }
}

/// Run `usermod -s` to set a user's login shell
pub fn run_usermod_shell<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    shell: &str,
) -> UserdResult<()> {
    let result = cmd_executor.execute(cmd![CMD_USERMOD, "-s", shell, username])?;
    if result.success() {
        debug!("Set shell for user {} to {}", username, shell);
        Ok(())
    } else {
        Err(result.to_error())
    }
}

/// Run `usermod -a -G` to add a user to a group (supplementary group)
pub fn run_usermod_add_group<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    group: &str,
) -> UserdResult<()> {
    let result = cmd_executor.execute(cmd![CMD_USERMOD, "-a", "-G", group, username])?;
    if result.success() {
        debug!("Added user {} to group {}", username, group);
        Ok(())
    } else {
        Err(result.to_error())
    }
}

/// Run `pkill` to send a signal to all processes owned by a user
///
/// # Arguments
/// * `cmd_executor` - Command executor to use
/// * `username` - Username whose processes to signal
/// * `signal` - Signal to send (e.g., "TERM", "KILL")
///
/// # Returns
/// * `Ok(true)` if processes were found and signaled
/// * `Ok(false)` if no processes were found (exit code 1)
/// * `Err` if the command failed to execute (exit code 2 or 3)
pub fn run_pkill<C: CommandExecutorTrait>(
    cmd_executor: &C,
    username: &str,
    signal: &str,
) -> UserdResult<bool> {
    let result = cmd_executor.execute(cmd![CMD_PKILL, &format!("-{}", signal), "-u", username])?;

    // pkill returns:
    // 0 - one or more processes matched and were signaled
    // 1 - no processes matched
    // 2 - syntax error
    // 3 - fatal error
    match result.code() {
        Some(0) => {
            debug!("Sent SIG{} to processes of user {}", signal, username);
            Ok(true)
        }
        Some(1) => {
            debug!("No processes found for user {} (already terminated)", username);
            Ok(false)
        }
        _ => {
            Err(result.to_error())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::UserdError;
    use crate::system::{CommandResult, MockCommandExecutor};
    use std::os::unix::process::ExitStatusExt;
    use std::process::ExitStatus;

    // Helper to create CommandResult for tests
    fn mock_result(status: ExitStatus, stdout: Vec<u8>, stderr: Vec<u8>) -> CommandResult {
        CommandResult {
            status,
            stdout,
            stderr,
            command_str: "test command".to_string(),
        }
    }

    // Helper to create CommandResult with custom command string
    fn mock_result_with_cmd(
        status: ExitStatus,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
        command_str: String,
    ) -> CommandResult {
        CommandResult {
            status,
            stdout,
            stderr,
            command_str,
        }
    }

    #[test]
    fn test_run_pkill_success() {
        let mut mock_cmd = MockCommandExecutor::new();

        // pkill returns 0 (success - processes were signaled)
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                Vec::new(),
                Vec::new(),
            ))
        });

        let result = run_pkill(&mock_cmd, "testuser", "TERM");
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), true); // Processes were found and signaled
    }

    #[test]
    fn test_run_pkill_no_processes_matched() {
        let mut mock_cmd = MockCommandExecutor::new();

        // pkill returns 1 (no processes matched - this is OK)
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                Vec::new(),
            ))
        });

        let result = run_pkill(&mock_cmd, "testuser", "KILL");
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), false); // No processes found
    }

    #[test]
    fn test_run_pkill_syntax_error() {
        let mut mock_cmd = MockCommandExecutor::new();

        // pkill returns 2 (syntax error)
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(512), // Exit code 2
                Vec::new(),
                b"pkill: invalid option".to_vec(),
            ))
        });

        let result = run_pkill(&mock_cmd, "testuser", "TERM");
        assert!(result.is_err());
        match result.unwrap_err() {
            UserdError::CommandError { command: _, message } => {
                assert!(message.contains("invalid option"));
            }
            _ => panic!("Expected CommandError"),
        }
    }

    #[test]
    fn test_run_pkill_fatal_error() {
        let mut mock_cmd = MockCommandExecutor::new();

        // pkill returns 3 (fatal error)
        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(768), // Exit code 3
                Vec::new(),
                b"pkill: fatal error".to_vec(),
            ))
        });

        let result = run_pkill(&mock_cmd, "testuser", "KILL");
        assert!(result.is_err());
        match result.unwrap_err() {
            UserdError::CommandError { command: _, message } => {
                assert!(message.contains("fatal error"));
            }
            _ => panic!("Expected CommandError"),
        }
    }

    #[test]
    fn test_password_masking_in_error_messages() {
        let mut mock_cmd = MockCommandExecutor::new();

        // Simulate usermod -p failure with masked password in command string
        mock_cmd.expect_execute_masked().returning(|_, _| {
            Ok(mock_result_with_cmd(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"usermod: user does not exist".to_vec(),
                "/usr/sbin/usermod -p *** testuser".to_string(), // Password is masked as ***
            ))
        });

        let result = run_usermod_password(&mock_cmd, "testuser", "$6$secret$hash");

        // Verify the error occurred
        assert!(result.is_err());

        // Verify the error message contains masked password, not the actual password
        match result.unwrap_err() {
            UserdError::CommandError { command, message } => {
                // Command should contain *** not the actual password
                assert!(command.contains("***"), "Command should mask password with ***");
                assert!(!command.contains("$6$secret$hash"), "Command should NOT contain actual password");
                assert!(command.contains("usermod"), "Command should contain usermod");
                assert!(command.contains("testuser"), "Command should contain username");
                assert!(message.contains("user does not exist"), "Message should contain error");
            }
            _ => panic!("Expected CommandError"),
        }
    }

    // ========== Tests for run_groups() ==========

    #[test]
    fn test_run_groups_success() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                b"testuser : sudo docker admin\n".to_vec(),
                Vec::new(),
            ))
        });

        let groups = run_groups(&mock_cmd, "testuser");

        assert_eq!(groups.len(), 3);
        assert_eq!(groups[0], "sudo");
        assert_eq!(groups[1], "docker");
        assert_eq!(groups[2], "admin");
    }

    #[test]
    fn test_run_groups_single_group() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                b"testuser : users\n".to_vec(),
                Vec::new(),
            ))
        });

        let groups = run_groups(&mock_cmd, "testuser");

        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0], "users");
    }

    #[test]
    fn test_run_groups_no_colon() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                b"invalid output\n".to_vec(),
                Vec::new(),
            ))
        });

        let groups = run_groups(&mock_cmd, "testuser");

        assert_eq!(groups.len(), 0);
    }

    #[test]
    fn test_run_groups_command_fails() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"groups: user not found".to_vec(),
            ))
        });

        let groups = run_groups(&mock_cmd, "testuser");

        assert_eq!(groups.len(), 0);
    }

    // ========== Tests for run_gpasswd_remove() ==========

    #[test]
    fn test_run_gpasswd_remove_success() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                Vec::new(),
                Vec::new(),
            ))
        });

        let result = run_gpasswd_remove(&mock_cmd, "testuser", "sudo");

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), GpasswdResult::Removed);
    }

    #[test]
    fn test_run_gpasswd_remove_not_a_member() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"gpasswd: user 'testuser' is not a member of 'sudo'\n".to_vec(),
            ))
        });

        let result = run_gpasswd_remove(&mock_cmd, "testuser", "sudo");

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), GpasswdResult::NotMember);
    }

    #[test]
    fn test_run_gpasswd_remove_other_error() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"gpasswd: group 'sudo' does not exist\n".to_vec(),
            ))
        });

        let result = run_gpasswd_remove(&mock_cmd, "testuser", "sudo");

        assert!(result.is_err());
        match result.unwrap_err() {
            UserdError::CommandError { message, .. } => {
                assert!(message.contains("does not exist"));
            }
            _ => panic!("Expected CommandError"),
        }
    }

    // ========== Tests for run_groupadd() ==========

    #[test]
    fn test_run_groupadd_success() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(0),
                Vec::new(),
                Vec::new(),
            ))
        });

        let result = run_groupadd(&mock_cmd, "testgroup");

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), true);
    }

    #[test]
    fn test_run_groupadd_already_exists() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"groupadd: group 'testgroup' already exists\n".to_vec(),
            ))
        });

        let result = run_groupadd(&mock_cmd, "testgroup");

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), true);
    }

    #[test]
    fn test_run_groupadd_other_error() {
        let mut mock_cmd = MockCommandExecutor::new();

        mock_cmd.expect_execute().returning(|_| {
            Ok(mock_result(
                ExitStatus::from_raw(256), // Exit code 1
                Vec::new(),
                b"groupadd: permission denied\n".to_vec(),
            ))
        });

        let result = run_groupadd(&mock_cmd, "testgroup");

        assert!(result.is_ok());
        assert_eq!(result.unwrap(), false);
    }
}

