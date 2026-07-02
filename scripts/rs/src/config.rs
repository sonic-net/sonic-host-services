use serde::Deserialize;
use std::{collections::HashMap, fs, time::Duration};

#[derive(Debug, Deserialize)]
pub struct OtelFileConfig {
    pub otel: OtelSection,
}

#[derive(Debug, Deserialize)]
pub struct OtelSection {
    pub endpoint: String,
    pub service_name: String,
    pub export_interval_secs: u64,

    #[serde(default = "default_true")]
    pub set_global: bool,

    #[serde(default)]
    pub resource: HashMap<String, String>,
}

fn default_true() -> bool {
    true
}

#[derive(Clone, Debug)]
pub struct OtelConfig {
    pub endpoint: String,
    pub service_name: String,
    pub export_interval: Duration,
    pub set_global: bool,
    pub resource_kvs: Vec<(String, String)>,
}

impl OtelConfig {
    pub fn from_toml_file(path: &str) -> anyhow::Result<Self> {
        let content = fs::read_to_string(path)?;
        let file_cfg: OtelFileConfig = toml::from_str(&content)?;

        Ok(Self {
            endpoint: file_cfg.otel.endpoint,
            service_name: file_cfg.otel.service_name,
            export_interval: Duration::from_secs(file_cfg.otel.export_interval_secs),
            set_global: file_cfg.otel.set_global,
            resource_kvs: file_cfg.otel.resource.into_iter().collect(),
        })
    }

    pub fn validate(&self) -> anyhow::Result<()> {
        if self.service_name.trim().is_empty() {
            anyhow::bail!("otel.service_name cannot be empty");
        }
        if !self.endpoint.starts_with("http://") && !self.endpoint.starts_with("https://") {
            anyhow::bail!(
                "otel.endpoint must start with http:// or https:// (got: {})",
                self.endpoint
            );
        }
        Ok(())
    }
}
