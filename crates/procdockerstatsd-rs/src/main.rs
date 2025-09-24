use std::process::Command;
use std::thread::sleep;
use std::time::Duration;
use swss_common::SonicV2Connector;
use regex::Regex;
use sysinfo::{System, Process, ProcessStatus};
use chrono::Utc;
use std::fs;
use std::collections::HashMap;
use std::sync::LazyLock;
use psutil;
use procfs;
use tracing::{error, info};
use tracing_subscriber::prelude::*;
use syslog_tracing;
use std::ffi::CString;


const UPDATE_INTERVAL: u64 = 120; // 2 minutes

struct ProcDockerStats {
    state_db: SonicV2Connector,
    system: System,
    process_cache: HashMap<u32, std::time::Instant>,
}


fn run_command(cmd: &[&str]) -> Option<String> {
    let output = Command::new(cmd[0]).args(&cmd[1..]).output().ok()?;
    if output.status.success() {
        Some(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        error!("Error running command: {:?}", cmd);
        None
    }
}

fn get_memory_percent(pid: u32) -> String {
    match psutil::process::Process::new(pid) {
        Ok(proc) => match proc.memory_percent() {
            Ok(mem_pct) => format!("{:.1}", mem_pct),
            Err(_) => String::new()
        },
        Err(_) => String::new()
    }
}

fn get_terminal_name(pid: u32) -> String {
    match procfs::process::Process::new(pid as i32) {
        Ok(proc) => match proc.stat() {
            Ok(stat) => {
                let (major, minor) = stat.tty_nr();
                if major == 0 && minor == 0 {
                    "?".to_string()
                } else {
                    format!("pts/{}", minor)
                }
            }
            Err(_) => "?".to_string()
        },
        Err(_) => "?".to_string()
    }
}

fn convert_to_bytes(value: &str) -> u64 {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(\d+\.?\d*)([a-zA-Z]+)").unwrap());
    if let Some(caps) = RE.captures(value) {
        let num: f64 = caps[1].parse().unwrap_or(0.0);
        let unit = &caps[2];
        match unit.to_lowercase().as_str() {
            "b" => num as u64,
            "kb" => (num * 1000.0) as u64,
            "mb" => (num * 1000.0 * 1000.0) as u64,
            "mib" => (num * 1024.0 * 1024.0) as u64,
            "gib" => (num * 1024.0 * 1024.0 * 1024.0) as u64,
            _ => num as u64,
        }
    } else {
        0
    }
}

fn format_docker_cmd_output(cmdout: &str) -> HashMap<String, HashMap<String, String>> {
    static MULTI_SPACE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"   +").unwrap());

    let lines: Vec<&str> = cmdout.lines().collect();
    if lines.len() < 2 { return HashMap::new(); }

    let keys: Vec<&str> = MULTI_SPACE_RE.split(lines[0]).collect();
    let mut docker_data_list = Vec::new();

    for line in &lines[1..] {
        let values: Vec<&str> = MULTI_SPACE_RE.split(line).collect();
        if values.len() >= keys.len() {
            let mut docker_data = HashMap::new();

            // Map values to keys just like Python
            for (key, value) in keys.iter().zip(values.iter()) {
                docker_data.insert(key.to_string(), value.to_string());
            }
            docker_data_list.push(docker_data);
        }
    }
    create_docker_dict(docker_data_list)
}

fn create_docker_dict(dict_list: Vec<HashMap<String, String>>) -> HashMap<String, HashMap<String, String>> {
    let mut dockerdict = HashMap::new();

    for row in dict_list {
        if let Some(cid) = row.get("CONTAINER ID") {
            let key = format!("DOCKER_STATS|{}", cid);
            let mut container_data = HashMap::new();

            if let Some(name) = row.get("NAME") {
                container_data.insert("NAME".to_string(), name.clone());
            }

            if let Some(cpu) = row.get("CPU %") {
                let cpu_clean = cpu.trim_end_matches('%');
                container_data.insert("CPU%".to_string(), cpu_clean.to_string());
            }

            if let Some(mem_usage) = row.get("MEM USAGE / LIMIT") {
                let memuse: Vec<&str> = mem_usage.split(" / ").collect();
                if memuse.len() >= 2 {
                    container_data.insert("MEM_BYTES".to_string(), convert_to_bytes(memuse[0]).to_string());
                    container_data.insert("MEM_LIMIT_BYTES".to_string(), convert_to_bytes(memuse[1]).to_string());
                }
            }

            if let Some(mem_pct) = row.get("MEM %") {
                let mem_clean = mem_pct.trim_end_matches('%');
                container_data.insert("MEM%".to_string(), mem_clean.to_string());
            }

            if let Some(net_io) = row.get("NET I/O") {
                let netio: Vec<&str> = net_io.split(" / ").collect();
                if netio.len() >= 2 {
                    container_data.insert("NET_IN_BYTES".to_string(), convert_to_bytes(netio[0]).to_string());
                    container_data.insert("NET_OUT_BYTES".to_string(), convert_to_bytes(netio[1]).to_string());
                }
            }

            if let Some(block_io) = row.get("BLOCK I/O") {
                let blockio: Vec<&str> = block_io.split(" / ").collect();
                if blockio.len() >= 2 {
                    container_data.insert("BLOCK_IN_BYTES".to_string(), convert_to_bytes(blockio[0]).to_string());
                    container_data.insert("BLOCK_OUT_BYTES".to_string(), convert_to_bytes(blockio[1]).to_string());
                }
            }

            if let Some(pids) = row.get("PIDS") {
                container_data.insert("PIDS".to_string(), pids.clone());
            }

            dockerdict.insert(key, container_data);
        }
    }
    dockerdict
}

impl ProcDockerStats {
    fn new() -> Result<Self, Box<dyn std::error::Error>> {
        let state_db = SonicV2Connector::new(false, None)?;
        state_db.connect("STATE_DB", true)?;

        Ok(ProcDockerStats {
            state_db,
            system: System::new_all(),
            process_cache: HashMap::new(),
        })
    }

    fn update_dockerstats_command(&mut self) -> Result<bool, Box<dyn std::error::Error>> {
        let cmd = ["docker", "stats", "--no-stream", "-a"];
        if let Some(output) = run_command(&cmd) {
            let stats_dict = format_docker_cmd_output(&output);
            if stats_dict.is_empty() {
                error!("formatting for docker output failed");
                return Ok(false);
            }
            self.state_db.delete_all_by_pattern("STATE_DB", "DOCKER_STATS|*")?;
            for (key, container_data) in stats_dict {
                // Convert the HashMap to a vector of tuples
                let stats_vec: Vec<(String, String)> = container_data.into_iter().collect();
                self.batch_update_state_db(&key, stats_vec)?;
            }
            Ok(true)
        } else {
            error!("'{:?}' returned null output", cmd);
            Ok(false)
        }
    }

    fn update_processstats_command(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        // Refresh system info like Python's process_iter
        self.system.refresh_all();

        let process_list: Vec<&Process> = self.system.processes().values().collect();

        // Sort processes by CPU usage with error handling for race conditions (like Python commit d409f27)
        let mut valid_processes = Vec::new();
        for process_obj in process_list {
            // Handle potential race condition where process might quit during CPU calculation
            match process_obj.status() {
                ProcessStatus::Unknown(_) | ProcessStatus::Zombie => continue,
                _ => {
                    let cpu = process_obj.cpu_usage();
                    valid_processes.push((cpu, process_obj));
                }
            }
        }
        valid_processes.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        let top_processes = valid_processes.iter().take(1024).map(|(_, p)| *p);

        let total_memory = self.system.total_memory() as f64;
        let mut pid_set = std::collections::HashSet::new();
        let mut processdata = Vec::new();

        // Collect all process data first before making mutable calls
        for process_obj in top_processes {
            // Add error handling similar to Python's try/except for NoSuchProcess, AccessDenied, ZombieProcess
            let pid = process_obj.pid().as_u32();
            pid_set.insert(pid);

            // Skip processes similar to Python exception handling: NoSuchProcess, AccessDenied, ZombieProcess
            match process_obj.status() {
                ProcessStatus::Unknown(_) | ProcessStatus::Zombie => continue,
                _ => {}
            }

            let value = format!("PROCESS_STATS|{}", pid);

            // Format STIME like Python: datetime.utcfromtimestamp(stime).strftime("%b%d")
            let stime_formatted = {
                let start_time = std::time::UNIX_EPOCH + std::time::Duration::from_secs(process_obj.start_time());
                let datetime: chrono::DateTime<chrono::Utc> = start_time.into();
                datetime.format("%b%d").to_string()
            };

            // Format TIME like Python: str(timedelta(seconds=int(ttime.user + ttime.system)))
            let time_formatted = {
                // Use accumulated_cpu_time() to get actual CPU time (user + system) in milliseconds, convert to seconds
                let total_seconds = process_obj.accumulated_cpu_time() / 1000; // Convert milliseconds to seconds
                let hours = total_seconds / 3600;
                let minutes = (total_seconds % 3600) / 60;
                let seconds = total_seconds % 60;
                // Python timedelta format: "H:MM:SS" or "M:SS" for values under 1 hour
                if hours > 0 {
                    format!("{}:{:02}:{:02}", hours, minutes, seconds)
                } else {
                    format!("{}:{:02}", minutes, seconds)
                }
            };

            // Safely access process fields that might fail
            let cmd = if process_obj.cmd().is_empty() {
                String::new()
            } else {
                process_obj.cmd().iter().map(|s| s.to_string_lossy()).collect::<Vec<_>>().join(" ")
            };

            let stats: Vec<(String, String)> = vec![
                ("PID".to_string(), pid.to_string()),
                ("UID".to_string(), process_obj.user_id().map(|uid| uid.to_string()).unwrap_or_else(|| "".to_string())),
                ("PPID".to_string(), process_obj.parent().map(|p| p.to_string()).unwrap_or_else(|| "".to_string())),
                ("%CPU".to_string(), format!("{:.2}", process_obj.cpu_usage() as f64)),
                ("%MEM".to_string(), format!("{:.1}", process_obj.memory() as f64 * 100.0 / total_memory)),
                ("STIME".to_string(), stime_formatted),
                ("TT".to_string(), get_terminal_name(pid)),
                ("TIME".to_string(), time_formatted), // CPU time like Python
                ("CMD".to_string(), cmd),
            ];

            processdata.push((value, stats));
        }

        // erase dead process - equivalent to Python lines 159-165
        let mut remove_keys = Vec::new();
        for &cached_pid in self.process_cache.keys() {
            if !pid_set.contains(&cached_pid) {
                remove_keys.push(cached_pid);
            }
        }
        for pid in remove_keys {
            self.process_cache.remove(&pid);
        }

        // Wipe out all data before updating with new values (like Python)
        self.state_db.delete_all_by_pattern("STATE_DB", "PROCESS_STATS|*")?;

        // Now make all the mutable calls after collecting the data
        for (value, stats) in processdata {
            self.batch_update_state_db(&value, stats)?;
        }

        Ok(())
    }

    fn update_fipsstats_command(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let kernel_cmdline = fs::read_to_string("/proc/cmdline").unwrap_or_default();
        let enforced = kernel_cmdline.contains("sonic_fips=1") || kernel_cmdline.contains("fips=1");

        // Check FIPS runtime status - simplified to match Python logic: not any(exitcode)
        let enabled = {
            match Command::new("sudo")
                .args(&["openssl", "engine", "-vv"])
                .output()
            {
                Ok(output) => {
                    if output.status.success() {
                        // Search for "symcryp" case-insensitive in the output
                        let stdout = String::from_utf8_lossy(&output.stdout);
                        stdout.to_lowercase().contains("symcryp")
                    } else {
                        false
                    }
                }
                Err(_) => false,
            }
        };

        let key = "FIPS_STATS|state";
        let mut stats = HashMap::new();
        stats.insert("timestamp".to_string(), Utc::now().format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()); // Match Python isoformat
        stats.insert("enforced".to_string(), enforced.to_string());
        stats.insert("enabled".to_string(), enabled.to_string());

        // Convert the HashMap to a vector of tuples
        let stats_vec: Vec<(String, String)> = stats.into_iter().collect();

        // Pass the vector of tuples to set
        self.batch_update_state_db(&key, stats_vec)?;

        Ok(())
    }

    fn update_state_db(&mut self, key1: &str, key2: &str, value2: &str) -> Result<(), Box<dyn std::error::Error>> {
        self.state_db.set("STATE_DB", key1, key2, value2, false)?;
        Ok(())
    }

    fn batch_update_state_db(&mut self, key1: &str, fvs: Vec<(String, String)>) -> Result<(), Box<dyn std::error::Error>> {
        self.state_db.hmset("STATE_DB", key1, fvs)?;
        Ok(())
    }

    fn run(&mut self) {
        // Check root privileges like Python version
        if unsafe { libc::getuid() } != 0 {
            error!("Must be root to run this daemon");
            std::process::exit(1);
        }

        info!("Started procdockerstatsd daemon");

        loop {
            self.update_dockerstats_command();
            let datetimeobj = Utc::now().format("%Y-%m-%d %H:%M:%S%.6f").to_string(); // Match Python str(datetime)
            self.update_state_db("DOCKER_STATS|LastUpdateTime", "lastupdate", &datetimeobj);

            self.update_processstats_command();
            self.update_state_db("PROCESS_STATS|LastUpdateTime", "lastupdate", &datetimeobj);

            self.update_fipsstats_command();
            self.update_state_db("FIPS_STATS|LastUpdateTime", "lastupdate", &datetimeobj);

            sleep(Duration::from_secs(UPDATE_INTERVAL));
        }
    }
}

fn main() {
    // Initialize tracing with syslog like sonic-ctrmgrd-rs example
    let identity = CString::new("procdockerstatsd").unwrap();
    let syslog = syslog_tracing::Syslog::new(
        identity,
        syslog_tracing::Options::LOG_PID,
        syslog_tracing::Facility::Daemon
    ).unwrap();
    tracing_subscriber::fmt()
        .with_writer(syslog)
        .with_ansi(false)
        .with_target(false)
        .with_level(false)
        .without_time()
        .init();

    info!("Starting up procdockerstatsd daemon");

    let mut daemon = ProcDockerStats::new().expect("Failed to initialize daemon");
    daemon.run();
}
