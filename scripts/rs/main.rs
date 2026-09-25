use std::process::Command;
use std::thread::sleep;
use std::time::Duration;
use redis::{Commands, Connection, Client};
use regex::Regex;
use sysinfo::{System, Process};
use chrono::Utc;
use std::fs;
use std::collections::HashMap;


const REDIS_URL: &str = "redis://127.0.0.1/";
const UPDATE_INTERVAL: u64 = 120; // 2 minutes


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
    let re = Regex::new(r"(\d+\.?\d*)([a-zA-Z]+)").unwrap();
    if let Some(caps) = re.captures(value) {
        let num: f64 = caps[1].parse().unwrap_or(0.0);
        let unit = &caps[2];
        match unit.to_lowercase().as_str() {
            "kb" => (num * 1024.0) as u64,
            "mb" | "mib" => (num * 1024.0 * 1024.0) as u64,
            "gb" | "gib" => (num * 1024.0 * 1024.0 * 1024.0) as u64,
            _ => num as u64,
        }
    } else {
        0
    }
}

fn parse_docker_stats(output: &str) -> Vec<HashMap<String, String>> {
    let lines: Vec<&str> = output.lines().collect();
    if lines.len() < 2 { return vec![]; }

    let keys: Vec<&str> = lines[0].split_whitespace().collect();
    let mut stats_list = Vec::new();
    
    for line in &lines[1..] {
        let values: Vec<&str> = line.split_whitespace().collect();
        if values.len() >= keys.len() {
            let mut stats = HashMap::new();
            stats.insert("CONTAINER ID".to_string(), values[0].to_string());
            stats.insert("NAME".to_string(), values[1].to_string());
            stats.insert("CPU%".to_string(), values[2].trim_end_matches('%').to_string());
            stats.insert("MEM_BYTES".to_string(), convert_to_bytes(values[3]).to_string());
            stats.insert("MEM_LIMIT_BYTES".to_string(), convert_to_bytes(values[5]).to_string());
            stats.insert("MEM%".to_string(), values[6].trim_end_matches('%').to_string());
            stats.insert("NET_IN_BYTES".to_string(), convert_to_bytes(values[7]).to_string());
            stats.insert("NET_OUT_BYTES".to_string(), convert_to_bytes(values[9]).to_string());
            stats.insert("BLOCK_IN_BYTES".to_string(), convert_to_bytes(values[10]).to_string());
            stats.insert("BLOCK_OUT_BYTES".to_string(), convert_to_bytes(values[12]).to_string());
            stats.insert("PIDS".to_string(), values[13].to_string());
            stats_list.push(stats);
        }
    }
    stats_list
}

fn collect_docker_stats(conn: &mut Connection) {
    if let Some(output) = run_command(&["docker", "stats", "--no-stream", "-a"]) {
        let stats_list = parse_docker_stats(&output);
        let _: () = redis::cmd("DEL").arg("DOCKER_STATS|*").execute(conn);
        for stats in stats_list {
            let key = format!("DOCKER_STATS|{}", stats["CONTAINER ID"]);
            
            // Convert the HashMap to a vector of tuples
            let stats_vec: Vec<(String, String)> = stats.into_iter().collect();
            let _: () = conn.hset_multiple(&key, &stats_vec).unwrap();
        }
    }
}

fn collect_process_stats(conn: &mut Connection) {
    let mut sys = System::new_all();
    sys.refresh_all();
    
    let mut process_list: Vec<&Process> = sys.processes().values().collect();
    
    // Sort processes by CPU usage in descending order and take top 1024
    process_list.sort_by(|a, b| b.cpu_usage().partial_cmp(&a.cpu_usage()).unwrap());
    let top_processes = process_list.iter().take(1024);
    
    let mut active_pids = std::collections::HashSet::new();
    
    for process in top_processes {
        let pid = process.pid().as_u32();
        active_pids.insert(pid);

        let key = format!("PROCESS_STATS|{}", pid);

        let stats: Vec<(String, String)> = vec![
            ("UID".to_string(), process.user_id().map(|uid| uid.to_string()).unwrap_or_else(|| "0".to_string())),
            ("PPID".to_string(), process.parent().map(|p| p.to_string()).unwrap_or_else(|| "0".to_string())),
            ("CMD".to_string(), process.cmd().join(" ")),
            ("CPU".to_string(), format!("{:.2}", process.cpu_usage() as f64)),
            ("MEM".to_string(), process.memory().to_string()),
            ("STIME".to_string(), process.start_time().to_string()),
        ];

        let _: () = conn.hset_multiple(&key, &stats).unwrap();
    }

    // Remove stale process stats from Redis
    let existing_keys: Vec<String> = conn.keys("PROCESS_STATS|*").unwrap_or_default();
    for key in existing_keys {
        if let Some(pid_str) = key.strip_prefix("PROCESS_STATS|") {
            if let Ok(pid) = pid_str.parse::<u32>() {
                if !active_pids.contains(&pid) {
                    let _: () = conn.del(&key).unwrap();
                }
            }
        }
    }
}


fn collect_fips_stats(conn: &mut Connection) {
    let kernel_cmdline = fs::read_to_string("/proc/cmdline").unwrap_or_default();
    let enforced = kernel_cmdline.contains("sonic_fips=1") || kernel_cmdline.contains("fips=1");
    let enabled = run_command(&["sudo", "openssl", "engine", "-vv"]).map_or(false, |out| out.contains("symcryp"));
    
    let key = "FIPS_STATS|state";
    let mut stats = HashMap::new();
    stats.insert("timestamp".to_string(), Utc::now().to_rfc3339());
    stats.insert("enforced".to_string(), enforced.to_string());
    stats.insert("enabled".to_string(), enabled.to_string());
    
    // Convert the HashMap to a vector of tuples
    let stats_vec: Vec<(String, String)> = stats.into_iter().collect();
    
    // Pass the vector of tuples to hset_multiple
    let _: () = conn.hset_multiple(&key, &stats_vec).unwrap();
}

fn main() {
    let client = Client::open(REDIS_URL).expect("Failed to connect to Redis");
    let mut conn = client.get_connection().expect("Failed to get Redis connection");
    
    loop {
        collect_docker_stats(&mut conn);
        collect_process_stats(&mut conn);
        collect_fips_stats(&mut conn);

        let timestamp = Utc::now().to_rfc3339();
        let _: () = conn.set("STATS|LastUpdateTime", timestamp).unwrap();
        sleep(Duration::from_secs(UPDATE_INTERVAL));
    }
}
