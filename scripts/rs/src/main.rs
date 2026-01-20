use opentelemetry::KeyValue;
use sysinfo::{CpuRefreshKind, ProcessRefreshKind, RefreshKind, System};
use tokio::time::{sleep, Duration};

fn find_main_dockerd_pid(system: &System) -> Option<u32> {
    // Prefer dockerd whose parent PID is 1 (your original heuristic)
    for (pid, process) in system.processes() {
        if process.name() == "dockerd"
            && process.parent().map(|pp| pp.as_u32()).unwrap_or(0) == 1
        {
            return Some(pid.as_u32());
        }
    }
    // Fallback: first dockerd found
    for (pid, process) in system.processes() {
        if process.name() == "dockerd" {
            return Some(pid.as_u32());
        }
    }
    None
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Config path: env override + default
    let cfg_path =
        std::env::var("OTEL_CONFIG_FILE").unwrap_or_else(|_| "otel.toml".to_string());

    let cfg = otel_lib_rs::OtelConfig::from_toml_file(&cfg_path)?;
    let otel = otel_lib_rs::OtelRuntime::init(cfg)?;

    let meter = otel.meter();

    let gauge = meter
        .f64_observable_gauge("dockerd.cpu.usage.percent")
        .with_description("Total CPU usage of dockerd process and its children (%)")
        .init();

    meter
        .register_callback(&[gauge.as_any()], move |observer| {
            let mut system = System::new_with_specifics(
                RefreshKind::new()
                    .with_processes(ProcessRefreshKind::everything())
                    .with_cpu(CpuRefreshKind::everything()),
            );

            // sysinfo CPU% is based on two samples
            system.refresh_processes();
            system.refresh_cpu();

            std::thread::sleep(std::time::Duration::from_millis(500));

            system.refresh_processes();
            system.refresh_cpu();

            let mut total_cpu = 0.0;

            if let Some(main_pid) = find_main_dockerd_pid(&system) {
                for (pid, process) in system.processes() {
                    if process.name() == "dockerd" {
                        let p = pid.as_u32();
                        let parent = process.parent().map(|pp| pp.as_u32()).unwrap_or(0);
                        if p == main_pid || parent == main_pid {
                            total_cpu += process.cpu_usage() as f64;
                        }
                    }
                }

                observer.observe_f64(
                    &gauge,
                    total_cpu,
                    &[
                        KeyValue::new("process", "dockerd"),
                        KeyValue::new("scope", "main+children"),
                    ],
                );
            } else {
                observer.observe_f64(
                    &gauge,
                    0.0,
                    &[
                        KeyValue::new("process", "dockerd"),
                        KeyValue::new("status", "not_found"),
                    ],
                );
            }
        })
        .expect("Failed to register callback");

    println!("Loaded OTEL config from: {}", cfg_path);
    println!("Exporting dockerd CPU usage...");

    loop {
        sleep(Duration::from_secs(600)).await;
    }
}
