//! Error types for userd daemon

use thiserror::Error;

/// Errors that can occur in the userd daemon
#[derive(Error, Debug)]
pub enum UserdError {
    /// Failed to execute a system command
    #[error("Failed to execute command '{command}': {message}")]
    CommandError { command: String, message: String },

    /// User not found in the system
    #[error("User not found: {0}")]
    UserNotFound(String),

    /// Invalid role specified
    #[error("Invalid role: {0}")]
    InvalidRole(String),

    /// Failed to read or write a file
    #[error("File I/O error for '{path}': {message}")]
    FileError { path: String, message: String },

    /// Database error from swss-common
    #[error("Database error: {0}")]
    DatabaseError(String),

    /// Configuration error
    #[error("Configuration error: {0}")]
    ConfigError(String),

    /// Permission denied
    #[error("Permission denied: {0}")]
    PermissionDenied(String),

    /// Invalid SSH key format
    #[error("Invalid SSH key format")]
    InvalidSshKey,

    /// System call error (from nix)
    #[error("System call error: {0}")]
    SystemError(#[from] nix::errno::Errno),

    /// I/O error
    #[error("I/O error: {0}")]
    IoError(#[from] std::io::Error),

    /// JSON parsing error
    #[error("JSON parsing error: {0}")]
    JsonError(#[from] serde_json::Error),
}

/// Result type alias for userd operations
pub type UserdResult<T> = Result<T, UserdError>;
