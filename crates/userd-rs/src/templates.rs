//! Template rendering for userd configuration files
//!
//! Uses j2cli for Jinja2 template rendering

use std::fs;
use std::io::Write;
use std::process::Command;

use serde_json::json;
use tempfile::NamedTempFile;
use tracing::{debug, error, info};

use crate::error::{UserdError, UserdResult};
use crate::types::{
    SecurityPolicy, CMD_CHMOD, CMD_J2CLI, PAM_AUTH_FAILLOCK_AUTHFAIL_CONF,
    PAM_AUTH_FAILLOCK_AUTHFAIL_TEMPLATE, PAM_AUTH_FAILLOCK_CONF, PAM_AUTH_FAILLOCK_TEMPLATE,
    PAM_FAILLOCK_CONF, PAM_FAILLOCK_TEMPLATE,
};

/// Build JSON template data for PAM faillock configuration
///
/// Creates the security_policies JSON structure expected by the faillock.conf.j2 template.
fn build_faillock_template_data(
    policies: &std::collections::HashMap<String, SecurityPolicy>,
) -> serde_json::Value {
    let mut policy_data = serde_json::Map::new();

    for policy in policies.values() {
        let mut role_data = serde_json::Map::new();
        role_data.insert(
            "max_login_attempts".to_string(),
            json!(policy.max_login_attempts),
        );
        policy_data.insert(policy.role.clone(), serde_json::Value::Object(role_data));
    }

    json!({
        "security_policies": policy_data
    })
}

/// Build JSON template data for PAM auth faillock configuration
///
/// Creates the template data with faillock_enabled flag and deny values for each role.
/// Uses defaults of 5 for missing roles or when max_login_attempts is 0.
fn build_auth_faillock_template_data(
    policies: &std::collections::HashMap<String, SecurityPolicy>,
) -> serde_json::Value {
    let faillock_enabled = !policies.is_empty();

    // Extract deny values for each role, with defaults
    let administrator_deny = policies
        .get("administrator")
        .map(|p| p.max_login_attempts)
        .filter(|&v| v > 0)
        .unwrap_or(5);

    let operator_deny = policies
        .get("operator")
        .map(|p| p.max_login_attempts)
        .filter(|&v| v > 0)
        .unwrap_or(5);

    json!({
        "faillock_enabled": faillock_enabled,
        "administrator_deny": administrator_deny,
        "operator_deny": operator_deny
    })
}

/// Render PAM faillock configuration from security policies
///
/// Creates a JSON file with security policies and renders the faillock.conf.j2
/// template using j2cli.
pub fn render_pam_faillock(
    policies: &std::collections::HashMap<String, SecurityPolicy>,
) -> UserdResult<()> {
    let template_data = build_faillock_template_data(policies);

    let temp_file = write_json_to_temp_file(&template_data, "security policies JSON")?;
    let rendered_content = render_template_with_j2(PAM_FAILLOCK_TEMPLATE, &temp_file)?;
    write_file_with_permissions(PAM_FAILLOCK_CONF, &rendered_content, 0o644)?;

    info!("Updated PAM faillock configuration using template");

    render_pam_auth_faillock(policies)?;

    Ok(())
}

/// Render PAM auth faillock configuration for common-auth integration
///
/// This generates /etc/pam.d/common-auth-faillock which is included via
/// substack from common-auth-sonic. When faillock is enabled, it adds
/// the pam_faillock.so preauth module with role-based deny values.
///
/// The PAM configuration:
/// 1. Skips faillock for non-managed users (not in local_mgd group)
/// 2. Applies administrator policy (deny=N) for users in sudo group
/// 3. Applies operator policy (deny=N) for users not in sudo group
pub fn render_pam_auth_faillock(
    policies: &std::collections::HashMap<String, SecurityPolicy>,
) -> UserdResult<()> {
    let template_data = build_auth_faillock_template_data(policies);

    let faillock_enabled = template_data["faillock_enabled"].as_bool().unwrap_or(false);
    let administrator_deny = template_data["administrator_deny"].as_u64().unwrap_or(5);
    let operator_deny = template_data["operator_deny"].as_u64().unwrap_or(5);

    let temp_file = write_json_to_temp_file(&template_data, "PAM auth faillock JSON")?;
    let rendered_content = render_template_with_j2(PAM_AUTH_FAILLOCK_TEMPLATE, &temp_file)?;
    write_file_with_permissions(PAM_AUTH_FAILLOCK_CONF, &rendered_content, 0o644)?;

    info!(
        "Updated PAM auth faillock preauth configuration (enabled={}, admin_deny={}, operator_deny={})",
        faillock_enabled, administrator_deny, operator_deny
    );

    render_pam_auth_faillock_authfail(&template_data)?;

    Ok(())
}

/// Render PAM auth faillock authfail configuration
///
/// This generates /etc/pam.d/common-auth-faillock-authfail which is included via
/// substack from common-auth-sonic AFTER authentication modules. When authentication
/// fails, this records the failed attempt in the faillock database.
fn render_pam_auth_faillock_authfail(template_data: &serde_json::Value) -> UserdResult<()> {
    let temp_file = write_json_to_temp_file(template_data, "PAM auth faillock authfail JSON")?;
    let rendered_content = render_template_with_j2(PAM_AUTH_FAILLOCK_AUTHFAIL_TEMPLATE, &temp_file)?;
    write_file_with_permissions(PAM_AUTH_FAILLOCK_AUTHFAIL_CONF, &rendered_content, 0o644)?;

    info!("Updated PAM auth faillock authfail configuration");
    Ok(())
}

/// Write JSON data to a temporary file
///
/// Creates a secure temporary file with "userd_" prefix and writes the JSON data to it.
/// The file is automatically cleaned up when the NamedTempFile is dropped.
///
/// Returns the NamedTempFile which must be kept alive while the file is needed.
fn write_json_to_temp_file(
    template_data: &serde_json::Value,
    description: &str,
) -> UserdResult<NamedTempFile> {
    let mut temp_file = NamedTempFile::with_prefix("userd_").map_err(|e| UserdError::FileError {
        path: "/tmp".to_string(),
        message: format!("Failed to create temp file: {}", e),
    })?;

    let json_str = serde_json::to_string(template_data)?;
    temp_file
        .write_all(json_str.as_bytes())
        .map_err(|e| UserdError::FileError {
            path: temp_file.path().display().to_string(),
            message: e.to_string(),
        })?;

    debug!("Wrote {} to {}", description, temp_file.path().display());

    Ok(temp_file)
}

/// Render a Jinja2 template using j2cli with JSON data
///
/// Takes a template file path and a temporary JSON data file, renders the template,
/// and returns the rendered content as a String.
fn render_template_with_j2(
    template_path: &str,
    json_file: &NamedTempFile,
) -> UserdResult<String> {
    let temp_path = json_file.path();
    let output = Command::new(CMD_J2CLI)
        .arg("--format=json")
        .arg(template_path)
        .arg(temp_path)
        .output()
        .map_err(|e| UserdError::CommandError {
            command: format!(
                "{} --format=json {} {}",
                CMD_J2CLI,
                template_path,
                temp_path.display()
            ),
            message: e.to_string(),
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        error!("j2 template rendering failed for {}: {}", template_path, stderr);
        return Err(UserdError::CommandError {
            command: CMD_J2CLI.to_string(),
            message: format!("Template rendering failed for {}: {}", template_path, stderr),
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

/// Write content to a file and set its permissions
///
/// Writes the content to the specified path and sets the file permissions using chmod.
fn write_file_with_permissions(
    path: &str,
    content: &str,
    mode: u32,
) -> UserdResult<()> {
    fs::write(path, content.as_bytes()).map_err(|e| UserdError::FileError {
        path: path.to_string(),
        message: e.to_string(),
    })?;

    set_file_permissions(path, mode)?;

    Ok(())
}

/// Set file permissions using chmod command
fn set_file_permissions(path: &str, mode: u32) -> UserdResult<()> {
    let mode_str = format!("{:o}", mode);
    let output = Command::new(CMD_CHMOD)
        .arg(&mode_str)
        .arg(path)
        .output()
        .map_err(|e| UserdError::CommandError {
            command: format!("{} {} {}", CMD_CHMOD, mode_str, path),
            message: e.to_string(),
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(UserdError::CommandError {
            command: format!("{} {} {}", CMD_CHMOD, mode_str, path),
            message: stderr.to_string(),
        });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    // ========== Tests for build_faillock_template_data() ==========

    #[test]
    fn test_faillock_json_with_single_policy() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 3,
            },
        );

        let template_data = build_faillock_template_data(&policies);

        assert_eq!(
            template_data["security_policies"]["administrator"]["max_login_attempts"],
            3
        );
    }

    #[test]
    fn test_faillock_json_with_multiple_policies() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 3,
            },
        );
        policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 5,
            },
        );

        let template_data = build_faillock_template_data(&policies);

        assert_eq!(
            template_data["security_policies"]["administrator"]["max_login_attempts"],
            3
        );
        assert_eq!(
            template_data["security_policies"]["operator"]["max_login_attempts"],
            5
        );
    }

    #[test]
    fn test_faillock_json_with_empty_policies() {
        let policies = HashMap::new();

        let template_data = build_faillock_template_data(&policies);

        assert!(template_data["security_policies"].is_object());
        assert_eq!(
            template_data["security_policies"]
                .as_object()
                .unwrap()
                .len(),
            0
        );
    }

    // ========== Tests for build_auth_faillock_template_data() ==========

    #[test]
    fn test_auth_faillock_json_enabled_when_policies_exist() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 3,
            },
        );

        let template_data = build_auth_faillock_template_data(&policies);

        assert_eq!(template_data["faillock_enabled"], true);
    }

    #[test]
    fn test_auth_faillock_json_disabled_when_policies_empty() {
        let policies = HashMap::new();

        let template_data = build_auth_faillock_template_data(&policies);

        assert_eq!(template_data["faillock_enabled"], false);
    }

    #[test]
    fn test_auth_faillock_json_uses_default_deny_when_missing() {
        let policies = HashMap::new();

        let template_data = build_auth_faillock_template_data(&policies);

        // Should use default of 5 when roles are missing
        assert_eq!(template_data["administrator_deny"], 5);
        assert_eq!(template_data["operator_deny"], 5);
    }

    #[test]
    fn test_auth_faillock_json_uses_default_deny_when_zero() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 0, // Disabled
            },
        );
        policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 0, // Disabled
            },
        );

        let template_data = build_auth_faillock_template_data(&policies);

        // Should use default of 5 when max_login_attempts is 0
        assert_eq!(template_data["administrator_deny"], 5);
        assert_eq!(template_data["operator_deny"], 5);
    }

    #[test]
    fn test_auth_faillock_json_uses_custom_deny_values() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 3,
            },
        );
        policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 7,
            },
        );

        let template_data = build_auth_faillock_template_data(&policies);

        assert_eq!(template_data["administrator_deny"], 3);
        assert_eq!(template_data["operator_deny"], 7);
    }

    #[test]
    fn test_auth_faillock_json_mixed_zero_and_custom() {
        let mut policies = HashMap::new();
        policies.insert(
            "administrator".to_string(),
            SecurityPolicy {
                role: "administrator".to_string(),
                max_login_attempts: 0, // Disabled - should use default
            },
        );
        policies.insert(
            "operator".to_string(),
            SecurityPolicy {
                role: "operator".to_string(),
                max_login_attempts: 10, // Custom value
            },
        );

        let template_data = build_auth_faillock_template_data(&policies);

        assert_eq!(template_data["administrator_deny"], 5); // Default
        assert_eq!(template_data["operator_deny"], 10); // Custom
    }

    // ========== Tests for write_json_to_temp_file() ==========

    #[test]
    fn test_write_json_to_temp_file_creates_file() {
        let data = json!({"test": "value"});

        let temp_file = write_json_to_temp_file(&data, "test data").unwrap();

        // Verify file exists
        assert!(temp_file.path().exists());
    }

    #[test]
    fn test_write_json_to_temp_file_contains_valid_json() {
        let data = json!({
            "key1": "value1",
            "key2": 42,
            "key3": true
        });

        let temp_file = write_json_to_temp_file(&data, "test data").unwrap();

        // Read the file content
        let content = fs::read_to_string(temp_file.path()).unwrap();

        // Verify it contains the expected JSON
        assert!(content.contains("\"key1\""));
        assert!(content.contains("\"value1\""));
        assert!(content.contains("\"key2\""));
        assert!(content.contains("42"));
        assert!(content.contains("\"key3\""));
        assert!(content.contains("true"));

        // Verify it's valid JSON by parsing it back
        let parsed: serde_json::Value = serde_json::from_str(&content).unwrap();
        assert_eq!(parsed["key1"], "value1");
        assert_eq!(parsed["key2"], 42);
        assert_eq!(parsed["key3"], true);
    }

    #[test]
    fn test_write_json_to_temp_file_cleanup_on_drop() {
        let data = json!({"test": "value"});
        let path;

        {
            let temp_file = write_json_to_temp_file(&data, "test data").unwrap();
            path = temp_file.path().to_path_buf();
            assert!(path.exists());
        } // temp_file is dropped here

        // Verify the file was cleaned up
        assert!(!path.exists());
    }

    #[test]
    fn test_write_json_to_temp_file_has_userd_prefix() {
        let data = json!({"test": "value"});

        let temp_file = write_json_to_temp_file(&data, "test data").unwrap();

        // Verify the filename has the "userd_" prefix
        let filename = temp_file.path().file_name().unwrap().to_str().unwrap();
        assert!(filename.starts_with("userd_"));
    }
}
