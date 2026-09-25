mod config;
pub use config::OtelConfig;

use opentelemetry::{global, KeyValue};
use opentelemetry::metrics::{Meter, MeterProvider as _};
use opentelemetry_otlp::WithExportConfig;

use opentelemetry_sdk::{
    metrics::{
        reader::{AggregationSelector, TemporalitySelector},
        Aggregation, InstrumentKind, MeterProviderBuilder, PeriodicReader,
    },
    runtime::Tokio,
    Resource,
};

pub struct OtelRuntime {
    meter: Meter,
    // Keep provider alive (dropping it can stop exports)
    _provider: opentelemetry_sdk::metrics::SdkMeterProvider,
}

struct LastValueCumulative;

impl AggregationSelector for LastValueCumulative {
    fn aggregation(&self, _kind: InstrumentKind) -> Aggregation {
        Aggregation::LastValue
    }
}

impl TemporalitySelector for LastValueCumulative {
    fn temporality(&self, _kind: InstrumentKind) -> opentelemetry_sdk::metrics::data::Temporality {
        opentelemetry_sdk::metrics::data::Temporality::Cumulative
    }
}

impl OtelRuntime {
    pub fn init(cfg: OtelConfig) -> anyhow::Result<Self> {
        cfg.validate()?;

        let exporter = opentelemetry_otlp::new_exporter()
            .tonic()
            .with_endpoint(cfg.endpoint)
            .build_metrics_exporter(Box::new(LastValueCumulative), Box::new(LastValueCumulative))
            .map_err(|e| anyhow::anyhow!("Failed to build OTLP metrics exporter: {e}"))?;

        let reader = PeriodicReader::builder(exporter, Tokio)
            .with_interval(cfg.export_interval)
            .build();

        let mut attrs = vec![KeyValue::new("service.name", cfg.service_name)];
        for (k, v) in cfg.resource_kvs {
            attrs.push(KeyValue::new(k, v));
        }

        let provider = MeterProviderBuilder::default()
            .with_resource(Resource::new(attrs))
            .with_reader(reader)
            .build();

        if cfg.set_global {
            global::set_meter_provider(provider.clone());
        }

        let meter = provider.meter("telemetry_rs");

        Ok(Self {
            meter,
            _provider: provider,
        })
    }

    pub fn meter(&self) -> &Meter {
        &self.meter
    }
}
