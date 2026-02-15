//! SONiC User Management Daemon (userd) entry point
//!
//! Monitors CONFIG_DB tables and manages local users accordingly.

use std::collections::HashMap;
use std::ffi::CString;
use std::time::{Duration, Instant};

use lazy_static::lazy_static;
use std::sync::Mutex;
use swss_common::{link_to_swsscommon_logger, DbConnector, KeyOperation, LoggerConfigChangeHandler, SubscriberStateTable};
use syslog_tracing;
use tokio::signal::unix::{signal, SignalKind};
use tracing::{error, info, warn};
use tracing_subscriber::{filter, prelude::*, reload, Layer, Registry};

use userd_rs::system::{CommandExecutor, SystemFunctions};
use userd_rs::types::*;
use userd_rs::user_manager::UserManager;

/// Program name used for logging configuration
const PROGRAM_NAME: &str = "userd";

/// Default log level for release builds
#[cfg(not(debug_assertions))]
const DEFAULT_LOG_LEVEL: filter::LevelFilter = filter::LevelFilter::INFO;

/// Default log level for debug builds
#[cfg(debug_assertions)]
const DEFAULT_LOG_LEVEL: filter::LevelFilter = filter::LevelFilter::DEBUG;

lazy_static! {
    /// Global handle to the reload layer for dynamic log level changes
    static ref LOG_LEVEL_HANDLE: Mutex<Option<reload::Handle<filter::LevelFilter, Registry>>> = Mutex::new(None);
}

/// Handler for CONFIG_DB Logger table changes
struct LoggerConfigHandler;

impl LoggerConfigChangeHandler for LoggerConfigHandler {
    fn on_log_level_change(&mut self, level: &str) {
        let new_level = match level.to_uppercase().as_str() {
            "EMERG" | "ALERT" | "CRIT" => filter::LevelFilter::ERROR,
            "ERROR" => filter::LevelFilter::ERROR,
            "WARNING" | "WARN" => filter::LevelFilter::WARN,
            "NOTICE" | "INFO" => filter::LevelFilter::INFO,
            "DEBUG" => filter::LevelFilter::DEBUG,
            _ => filter::LevelFilter::INFO,
        };

        if let Ok(guard) = LOG_LEVEL_HANDLE.lock() {
            if let Some(ref handle) = *guard {
                if let Err(e) = handle.modify(|f| *f = new_level) {
                    eprintln!("Failed to update log level: {}", e);
                } else {
                    info!("Log level changed to: {} (mapped to {:?})", level, new_level);
                }
            }
        }
    }

    fn on_log_output_change(&mut self, output: &str) {
        // Rust doesn't support dynamically changing log output
        // We only support syslog output
        if output.to_uppercase() != "SYSLOG" {
            info!(
                "Log output change to unsupported destination {}. Setting ignored",
                output
            );
        }
    }
}

/// Initialize logging with support for:
/// 1. RUST_LOG or USERD_LOG_LEVEL environment variables (highest priority)
/// 2. CONFIG_DB Logger table for dynamic log level changes (if env not set)
fn init_logging() -> Result<(), Box<dyn std::error::Error>> {
    let identity = CString::new(PROGRAM_NAME).map_err(|e| format!("invalid identity string: {}", e))?;
    let syslog = syslog_tracing::Syslog::new(
        identity,
        syslog_tracing::Options::LOG_PID,
        syslog_tracing::Facility::Daemon,
    )
    .ok_or("failed to initialize syslog")?;

    // Check if log level is set via environment variable
    let env_log_level = std::env::var("RUST_LOG")
        .or_else(|_| std::env::var("USERD_LOG_LEVEL"));

    let log_env_set = env_log_level.is_ok();

    if log_env_set {
        // Use EnvFilter for environment variable based logging
        let filter = tracing_subscriber::filter::EnvFilter::try_from_default_env()
            .or_else(|_| tracing_subscriber::filter::EnvFilter::try_new(
                env_log_level.unwrap_or_else(|_| "info".to_string())
            ))
            .unwrap_or_else(|_| tracing_subscriber::filter::EnvFilter::new("info"));

        let syslog_layer = tracing_subscriber::fmt::layer()
            .with_writer(syslog)
            .with_ansi(false)
            .with_target(false)
            .with_level(false)
            .without_time();

        tracing_subscriber::registry()
            .with(filter.and_then(syslog_layer))
            .init();
    } else {
        // Use CONFIG_DB Logger table with reload support for dynamic log level changes
        let (level_layer, level_reload_handle) = reload::Layer::new(DEFAULT_LOG_LEVEL);

        // Store the handle globally for the LoggerConfigHandler to use
        {
            let mut guard = LOG_LEVEL_HANDLE.lock().map_err(|e| format!("Failed to lock: {}", e))?;
            *guard = Some(level_reload_handle);
        }

        let syslog_layer = tracing_subscriber::fmt::layer()
            .with_writer(syslog)
            .with_ansi(false)
            .with_target(false)
            .with_level(false)
            .without_time();

        tracing_subscriber::registry()
            .with(level_layer.and_then(syslog_layer))
            .init();

        // Link to CONFIG_DB Logger table for dynamic log level changes
        let handler = LoggerConfigHandler;
        if let Err(e) = link_to_swsscommon_logger(PROGRAM_NAME, handler) {
            // Don't fail if we can't link to swsscommon logger, just log a warning
            eprintln!("Warning: Unable to link to CONFIG_DB Logger table: {}. Using default log level.", e);
        }
    }

    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging with environment variable and CONFIG_DB support
    init_logging()?;

    info!("Starting userd daemon");

    // Check root privileges
    if unsafe { libc::getuid() } != 0 {
        error!("Must be root to run this daemon");
        std::process::exit(1);
    }

    // Setup signal handlers
    let mut sigterm = signal(SignalKind::terminate())?;
    let mut sigint = signal(SignalKind::interrupt())?;

    // Create system abstractions
    let cmd_executor = CommandExecutor;
    let sys_funcs = SystemFunctions;

    // Create UserManager
    let mut user_manager = UserManager::new(cmd_executor, sys_funcs);

    // Create subscriber tables for monitoring
    // Each SubscriberStateTable needs its own DbConnector since it takes ownership
    let user_db = DbConnector::new_named("CONFIG_DB", false, 0)
        .map_err(|e| format!("Failed to connect to CONFIG_DB for user subscription: {}", e))?;
    let mut user_table = SubscriberStateTable::new(user_db, LOCAL_USER_TABLE, None, None)
        .map_err(|e| format!("Failed to create LOCAL_USER subscriber: {}", e))?;

    let policy_db = DbConnector::new_named("CONFIG_DB", false, 0)
        .map_err(|e| format!("Failed to connect to CONFIG_DB for policy subscription: {}", e))?;
    let mut policy_table = SubscriberStateTable::new(policy_db, LOCAL_ROLE_SECURITY_POLICY_TABLE, None, None)
        .map_err(|e| format!("Failed to create SECURITY_POLICY subscriber: {}", e))?;

    let metadata_db = DbConnector::new_named("CONFIG_DB", false, 0)
        .map_err(|e| format!("Failed to connect to CONFIG_DB for metadata subscription: {}", e))?;
    let mut metadata_table = SubscriberStateTable::new(metadata_db, DEVICE_METADATA_TABLE, None, None)
        .map_err(|e| format!("Failed to create DEVICE_METADATA subscriber: {}", e))?;

    // Initial drain from all SubscriberStateTables
    let timer1 = process_entries(&mut user_table, LOCAL_USER_TABLE, |key, data| {
       user_manager.handle_user_change(key, data)
    });
    let timer2 = process_entries(&mut policy_table, LOCAL_ROLE_SECURITY_POLICY_TABLE, |key, data| {
       user_manager.handle_policy_change(key, data)
    });
    let timer3 = process_entries(&mut metadata_table, DEVICE_METADATA_TABLE, |key, data| {
        user_manager.handle_metadata_change(key, data)
    });
    user_manager.set_initial_read_done();

    // Timer for pending deletion processing
    // Start with the earliest timer from initial processing, or 60 seconds
    let mut next_deletion_check = [timer1, timer2, timer3]
        .iter()
        .filter_map(|&t| t)
        .min()
        .unwrap_or_else(|| Instant::now() + Duration::from_secs(60));

    // Main event loop - uses async I/O to wait for events efficiently
    loop {
        tokio::select! {
            _ = sigterm.recv() => {
                info!("Received SIGTERM, shutting down");
                break;
            }
            _ = sigint.recv() => {
                info!("Received SIGINT, shutting down");
                break;
            }
            _ = tokio::time::sleep_until(tokio::time::Instant::from_std(next_deletion_check)) => {
                next_deletion_check = user_manager.process_pending_deletions();
            }
            result = user_table.read_data_async() => {
                if let Err(e) = result {
                    warn!("Failed to read from {}: {}", LOCAL_USER_TABLE, e);
                    continue;
                }
                if let Some(timer) = process_entries(&mut user_table, LOCAL_USER_TABLE, |key, data| {
                    user_manager.handle_user_change(key, data)
                }) {
                    // Update timer if a pending deletion was added
                    next_deletion_check = next_deletion_check.min(timer);
                }
            }
            result = policy_table.read_data_async() => {
                if let Err(e) = result {
                    warn!("Failed to read from {}: {}", LOCAL_ROLE_SECURITY_POLICY_TABLE, e);
                    continue;
                }
                let _ = process_entries(&mut policy_table, LOCAL_ROLE_SECURITY_POLICY_TABLE, |key, data| {
                    user_manager.handle_policy_change(key, data)
                });
            }
            result = metadata_table.read_data_async() => {
                if let Err(e) = result {
                    warn!("Failed to read from {}: {}", DEVICE_METADATA_TABLE, e);
                    continue;
                }
                let _ = process_entries(&mut metadata_table, DEVICE_METADATA_TABLE, |key, data| {
                    user_manager.handle_metadata_change(key, data)
                });
            }
        }
    }

    info!("userd daemon shutdown complete");
    Ok(())
}

/// Extract entry data from a SubscriberStateTable entry
fn extract_entry_data(entry: &swss_common::KeyOpFieldValues) -> HashMap<String, String> {
    if entry.operation == KeyOperation::Del {
        HashMap::new()
    } else {
        entry
            .field_values
            .iter()
            .map(|(k, v)| (k.clone(), v.to_string_lossy().into_owned()))
            .collect()
    }
}

/// Process entries from a SubscriberStateTable using the provided handler
/// Returns the minimum timer value from all handlers, if any
fn process_entries<F>(table: &mut SubscriberStateTable, table_name: &str, mut handler: F) -> Option<Instant>
where
    F: FnMut(&str, &HashMap<String, String>) -> userd_rs::UserdResult<Option<Instant>>,
{
    let mut min_timer: Option<Instant> = None;

    match table.pops() {
        Ok(entries) => {
            for entry in entries {
                let data = extract_entry_data(&entry);
                match handler(&entry.key, &data) {
                    Ok(Some(timer)) => {
                        min_timer = Some(match min_timer {
                            Some(current) => current.min(timer),
                            None => timer,
                        });
                    }
                    Ok(None) => {}
                    Err(e) => {
                        error!("Failed to handle {} change for {}: {}", table_name, entry.key, e);
                    }
                }
            }
        }
        Err(e) => {
            warn!("Failed to pop entries from {}: {}", table_name, e);
        }
    }

    min_timer
}
