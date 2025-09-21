use std::process::Command;
use std::thread::sleep;
use std::time::Duration;
use redis::{Commands, Connection, Client};
use regex::Regex;
use sysinfo::{System, Process, Pid};
use chrono::{Utc, DateTime};
use std::fs;
use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::UNIX_EPOCH;


const REDIS_URL: &str = "redis://127.0.0.1/";
const UPDATE_INTERVAL: u64 = 120; // 2 minutes

struct ProcDockerStats {
    redis_conn: Connection,
    system: System,
    process_cache: HashMap<u32, std::time::Instant>,
}


fn run_command(cmd: &[&str]) -> Option<String> {
    let output = Command::new(cmd[0]).args(&cmd[1..]).output().ok()?;
    if output.status.success() {
        Some(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        eprintln!("Error running command: {:?}", cmd);
        None
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
        // Check root privileges like Python version
        if unsafe { libc::getuid() } != 0 {
            eprintln!("Must be root to run this daemon");
            std::process::exit(1);
        }

        let client = Client::open(REDIS_URL)?;
        let conn = client.get_connection()?;

        Ok(ProcDockerStats {
            redis_conn: conn,
            system: System::new_all(),
            process_cache: HashMap::new(),
        })
    }

    fn update_dockerstats_command(&mut self) -> bool {
        let cmd = ["docker", "stats", "--no-stream", "-a"];
        if let Some(output) = run_command(&cmd) {
            let stats_dict = format_docker_cmd_output(&output);
            if stats_dict.is_empty() {
                eprintln!("formatting for docker output failed");
                return false;
            }
            let _: () = redis::cmd("DEL").arg("DOCKER_STATS|*").execute(&mut self.redis_conn);
            for (key, container_data) in stats_dict {
                // Convert the HashMap to a vector of tuples
                let stats_vec: Vec<(String, String)> = container_data.into_iter().collect();
                self.batch_update_state_db(&key, stats_vec);
            }
            true
        } else {
            eprintln!("'{:?}' returned null output", cmd);
            false
        }
    }

    fn update_processstats_command(&mut self) {
        // Refresh system info like Python's process_iter
        self.system.refresh_all();

        let mut process_list: Vec<&Process> = self.system.processes().values().collect();

        // Sort processes by CPU usage in descending order and take top 1024
        process_list.sort_by(|a, b| b.cpu_usage().partial_cmp(&a.cpu_usage()).unwrap());
        let top_processes = process_list.iter().take(1024);

        let mut active_pids = std::collections::HashSet::new();

        // Clear stale processes from cache first like Python does
        for process in top_processes {
            // Add error handling similar to Python's try/except for NoSuchProcess, AccessDenied, ZombieProcess
            let pid = process.pid().as_u32();
            active_pids.insert(pid);

            // Skip processes that no longer exist or we can't access
            if !process.status().is_some() {
                continue;
            }

            let key = format!("PROCESS_STATS|{}", pid);

            // Format STIME like Python: datetime.utcfromtimestamp(stime).strftime("%b%d")
            let stime_formatted = {
                let start_time = std::time::UNIX_EPOCH + std::time::Duration::from_secs(process.start_time());
                let datetime: chrono::DateTime<chrono::Utc> = start_time.into();
                datetime.format("%b%d").to_string()
            };

            // Format TIME like Python: str(timedelta(seconds=int(ttime.user + ttime.system)))
            let time_formatted = {
                let cpu_time = process.cpu_time();
                let total_seconds = cpu_time as u64;
                let hours = total_seconds / 3600;
                let minutes = (total_seconds % 3600) / 60;
                let seconds = total_seconds % 60;
                if hours > 0 {
                    format!("{}:{:02}:{:02}", hours, minutes, seconds)
                } else {
                    format!("{}:{:02}", minutes, seconds)
                }
            };

            // Safely access process fields that might fail
            let cmd_string = if process.cmd().is_empty() {
                String::new()
            } else {
                process.cmd().join(" ")
            };

            let stats: Vec<(String, String)> = vec![
                ("PID".to_string(), pid.to_string()),
                ("UID".to_string(), process.user_id().map(|uid| uid.to_string()).unwrap_or_else(|| "0".to_string())),
                ("PPID".to_string(), process.parent().map(|p| p.to_string()).unwrap_or_else(|| "0".to_string())),
                ("CPU".to_string(), format!("{:.2}", process.cpu_usage() as f64)),
                ("MEM".to_string(), format!("{:.1}", process.memory_percent())), // Memory as percentage like Python
                ("STIME".to_string(), stime_formatted),
                ("TT".to_string(), process.terminal().unwrap_or("?").to_string()), // Terminal like Python
                ("TIME".to_string(), time_formatted), // CPU time like Python
                ("CMD".to_string(), cmd_string),
            ];

            self.batch_update_state_db(&key, stats);
        }

    // Remove stale process stats from Redis
    let existing_keys: Vec<String> = self.redis_conn.keys("PROCESS_STATS|*").unwrap_or_default();
    for key in existing_keys {
        if let Some(pid_str) = key.strip_prefix("PROCESS_STATS|") {
            if let Ok(pid) = pid_str.parse::<u32>() {
                if !active_pids.contains(&pid) {
                    let _: () = self.redis_conn.del(&key).unwrap();
                }
            }
        }
    }
}

    fn update_fipsstats_command(&mut self) {
        let kernel_cmdline = fs::read_to_string("/proc/cmdline").unwrap_or_default();
        let enforced = kernel_cmdline.contains("sonic_fips=1") || kernel_cmdline.contains("fips=1");

        // Check FIPS runtime status using pipe like Python: openssl engine -vv | grep -i symcryp
        let enabled = {
            let openssl_output = Command::new("sudo")
                .args(&["openssl", "engine", "-vv"])
                .output()
                .ok();

            if let Some(output) = openssl_output {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let grep_output = Command::new("grep")
                    .args(&["-i", "symcryp"])
                    .stdin(std::process::Stdio::piped())
                    .stdout(std::process::Stdio::piped())
                    .spawn()
                    .and_then(|mut child| {
                        use std::io::Write;
                        if let Some(stdin) = child.stdin.as_mut() {
                            let _ = stdin.write_all(stdout.as_bytes());
                        }
                        child.wait_with_output()
                    });

                grep_output.map_or(false, |output| output.status.success())
            } else {
                false
            }
        };

        let key = "FIPS_STATS|state";
        let mut stats = HashMap::new();
        stats.insert("timestamp".to_string(), Utc::now().format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()); // Match Python isoformat
        stats.insert("enforced".to_string(), enforced.to_string());
        stats.insert("enabled".to_string(), enabled.to_string());

        // Convert the HashMap to a vector of tuples
        let stats_vec: Vec<(String, String)> = stats.into_iter().collect();

        // Pass the vector of tuples to hset_multiple
        let _: () = self.batch_update_state_db(&key, stats_vec);
    }

    fn update_state_db(&mut self, key1: &str, key2: &str, value2: &str) {
        let _: () = self.redis_conn.hset(key1, key2, value2).unwrap();
    }

    fn batch_update_state_db(&mut self, key1: &str, fvs: Vec<(String, String)>) {
        let _: () = self.redis_conn.hset_multiple(key1, &fvs).unwrap();
    }

    fn run(&mut self) {
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
    let mut daemon = ProcDockerStats::new().expect("Failed to initialize daemon");
    daemon.run();
}
