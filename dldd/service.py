"""DLDD process composition and primary service loop."""

from __future__ import annotations

import logging
import json
import signal
import threading
import time
from queue import Queue
from typing import Dict, List, Mapping, Optional, Tuple

from .actions import ActionExecutor, ActionRunner
from .adapters import DataSourceAdapter
from .artifacts import (
    DEFAULT_ARTIFACT_DIRECTORY,
    FilesystemArtifactClient,
    HealthzArtifactClient,
)
from .config import ConfigDBProvider, DLDDConfig, load_vendor_defaults
from .correlation import CorrelationEngine
from .hooks import operation_hook_name
from .lifecycle import (
    ActivationResult,
    BrokenRuleStateStore,
    CandidateValidation,
    NoRulesAvailable,
    RuleGenerationManager,
    RulePaths,
)
from .monitor import AsyncCollectionPool, MonitorThread
from .orchestrator import PrimaryOrchestrator
from .planner import build_plans
from .platform import PlatformExtensions, detect_identity, load_extensions
from .preflight import (
    ActivationPreflightResult,
    build_adapter_registry,
    preflight_activation,
)
from .runtime import make_rule_instance_id
from .rule_schema.errors import bound_diagnostic, bound_identity
from .telemetry import SonicStateDB, TelemetryPublisher
from .timestamps import floor_timestamp
from .validation import (
    MAX_SERIALIZED_DIAGNOSTIC_BYTES,
    ValidationContext,
    load_rules,
)


LOGGER = logging.getLogger(__name__)


class TelemetryUnavailable(RuntimeError):
    """Raised after bounded STATE_DB publication retries are exhausted."""


TELEMETRY_FAILURE_LIMIT = 3
TELEMETRY_RETRY_INTERVAL = 1


_SCHEMA_ISSUE_CODES = frozenset(
    (
        "duplicate_event_id",
        "duplicate_instance",
        "empty_log_collection",
        "invalid_argv",
        "invalid_events",
        "invalid_field",
        "invalid_hex",
        "invalid_logs",
        "invalid_priority",
        "invalid_queries",
        "invalid_rule_name",
        "invalid_semver",
        "invalid_signature",
        "invalid_type",
        "invalid_value",
        "instance_path_mismatch",
        "instance_value_mismatch",
        "invalid_format",
        "invalid_length",
        "missing_i2c_value",
        "missing_field",
        "out_of_range",
        "reserved_operation_field",
        "reserved_operation_type",
        "unsupported_component",
        "unsupported_severity",
        "unsupported_type",
        "unsupported_value",
        "unsupported_value_type",
        "unknown_field",
    )
)


def _ingestion_failure_reason(issues) -> str:
    """Return a stable category prefix plus the rule diagnostics."""

    details = "; ".join(str(issue) for issue in issues)
    searchable = " ".join(
        "{} {}".format(issue.code, issue.message).lower() for issue in issues
    )
    issue_codes = {issue.code for issue in issues}
    if "dse" in searchable:
        category = "dse_error"
    elif any(
        token in searchable
        for token in ("evaluation", "evaluator", "operator", "logic", "mask", "regex")
    ):
        category = "evaluation_error"
    elif issue_codes & _SCHEMA_ISSUE_CODES:
        category = "schema_error"
    else:
        category = "validation_error"
    return bound_diagnostic("{}: {}".format(category, details), 4096)


def _compact_json_size(value) -> int:
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", "replace"
        )
    )


def _operator_status_records(records, work_items=None):
    """Project internal records onto the public rule-instance identity."""

    work_items = work_items or {}
    result = []
    for record in records:
        if not isinstance(record, Mapping):
            result.append(record)
            continue
        public = dict(record)
        correlation_key = public.pop("correlation_key", "")
        item = work_items.get(correlation_key)
        rule_id = public.get("rule_id")
        component_name = public.get(
            "component_name", public.get("component", "")
        )
        if item is not None:
            if rule_id in (None, ""):
                rule_id = item.rule_id
                public["rule_id"] = rule_id
            if not component_name:
                component_name = item.component_name
            public.setdefault("component_type", item.component_type)
        if component_name:
            public.pop("component", None)
            public["component_name"] = component_name
        if (
            not public.get("rule_instance_id")
            and rule_id not in (None, "")
            and component_name
        ):
            public["rule_instance_id"] = make_rule_instance_id(
                rule_id, component_name
            )
        result.append(public)
    return tuple(result)


def _bounded_broken_rule_records(result, validation_time):
    """Project every broken identity within the external candidate budget."""

    records = [
        {
            "rule": bound_identity(item.rule_name, 128),
            "rule_id": item.rule_id,
            "version": bound_identity(item.rule_version, 64),
            "state": "BROKEN",
            "reason": _ingestion_failure_reason(item.issues),
            "failure_count": 1,
            "last_attempt": floor_timestamp(validation_time),
        }
        for item in result.broken_rules
    ]
    budget = MAX_SERIALIZED_DIAGNOSTIC_BYTES - 32 * 1024
    if _compact_json_size(records) <= budget:
        return tuple(records)

    original_reasons = [record["reason"] for record in records]
    for maximum in (1024, 512, 256, 128, 64):
        for record, reason in zip(records, original_reasons):
            record["reason"] = bound_diagnostic(reason, maximum)
        if _compact_json_size(records) <= budget:
            return tuple(records)

        # Preserve every broken-rule identity when details exceed the byte cap.
    for record in records:
        record["reason"] = "validation details omitted by candidate byte cap"
    return tuple(records)


def _bounded_file_error_strings(issues):
    selected = [bound_diagnostic(str(issue), 1024) for issue in issues[:256]]
    if len(issues) > len(selected):
        selected.append("additional file diagnostics were omitted")
    return tuple(selected)


class DLDDService:
    """Compose and supervise the complete device-local diagnosis runtime."""

    def __init__(
        self,
        paths: Optional[RulePaths] = None,
        config_db=None,
        state_db=None,
        extensions: Optional[PlatformExtensions] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        identity = extensions.identity if extensions else detect_identity()
        platform_dir = "/usr/share/sonic/device/{}".format(identity.platform)
        self.paths = paths or RulePaths(platform_dir=platform_dir)
        self.extensions = extensions or load_extensions(identity, self.paths.dse)
        self.config_provider = ConfigDBProvider(config_db)
        self.state_db = state_db or SonicStateDB()
        self.stop_event = stop_event or threading.Event()
        self.config = DLDDConfig()
        self.telemetry: Optional[TelemetryPublisher] = None
        self.activation: Optional[ActivationResult] = None
        self.monitors: List[MonitorThread] = []
        self.orchestrator: Optional[PrimaryOrchestrator] = None
        self.action_runner: Optional[ActionRunner] = None
        self.async_collection_pool: Optional[AsyncCollectionPool] = None
        self.artifact_client: Optional[HealthzArtifactClient] = None
        self.adapters: Optional[Mapping[str, DataSourceAdapter]] = None
        self.evidence_queue: Optional[Queue] = None
        self.state_store = BrokenRuleStateStore(self.paths.state_file)
        self.config_thread: Optional[threading.Thread] = None
        self._state_fingerprint: Optional[str] = None
        self.fatal_reason = ""
        self.startup_broken = ()
        self._serial_cache: Dict[Tuple[str, str], str] = {}

    def _load_config(self) -> DLDDConfig:
        try:
            config_db = self.config_provider.load()
        except Exception as error:
            LOGGER.warning("unable to load DLDD_CONFIG: %s", error)
            config_db = {}
        try:
            defaults = load_vendor_defaults(self.paths.defaults)
        except Exception as error:
            LOGGER.error("invalid vendor defaults: %s", error)
            defaults = {}
        return DLDDConfig.from_sources(config_db, defaults)

    def _validate_candidate(self, path: str, dse_path: str) -> CandidateValidation:
        context = ValidationContext(
            product_id=self.extensions.identity.product_id,
            software_version=self.extensions.identity.software_version,
            require_compatibility_identity=True,
            dse_registry=self.extensions.dse_registry,
            compatibility_matcher=self.extensions.compatibility_matcher,
        )
        result = load_rules(path, context)
        payload = result
        if result.materialized_rules:
            preflight = preflight_activation(
                result,
                self.extensions,
                self.config.polling_intervals,
                failure_message_limit=256,
            )
            result, payload = preflight.validation, preflight
        validation_time = time.time()
        broken = _bounded_broken_rule_records(result, validation_time)
        errors = _bounded_file_error_strings(result.file_errors)
        if not result.materialized_rules and result.broken_rules:
            errors += (
                "zero usable rules; see {} bounded broken-rule diagnostics".format(
                    len(result.broken_rules)
                ),
            )
        return CandidateValidation(
            file_valid=result.file_valid,
            usable_rule_count=len(result.materialized_rules),
            schema_version=result.schema_version or "",
            broken_rules=broken,
            errors=errors,
            payload=payload,
        )

    def _adapters(self):
        payload = getattr(self.activation, "payload", None)
        return payload.adapters if isinstance(
            payload, ActivationPreflightResult
        ) else build_adapter_registry(self.extensions)

    def _fail_start(self, reason: str) -> None:
        """Record and publish one fatal startup outcome."""

        self.fatal_reason = str(reason)
        if self.activation is not None:
            self.startup_broken = tuple(self.activation.broken_rules)
        self._publish_status()

    def _new_monitor(self, plan) -> MonitorThread:
        """Construct a monitor using the active shared runtime dependencies."""

        if self.adapters is None or self.evidence_queue is None:
            raise RuntimeError("monitor dependencies are unavailable")
        return MonitorThread(
            plan,
            self.adapters,
            self.evidence_queue,
            fault_evidence_ack_timeout=self.config.fault_evidence_ack_timeout,
            source_recovery_samples=self.config.source_recovery_samples,
            async_collection_pool=self.async_collection_pool,
            stop_event=self.stop_event,
        )

    def _create_artifact_client(self) -> HealthzArtifactClient:
        factory = getattr(self.extensions, "artifact_client_factory", None)
        if factory is None:
            return FilesystemArtifactClient(
                directory=DEFAULT_ARTIFACT_DIRECTORY,
                query_runner=self._run_artifact_query,
            )
        client = factory(
            identity=self.extensions.identity,
            artifact_directory=DEFAULT_ARTIFACT_DIRECTORY,
            query_runner=self._run_artifact_query,
        )
        if not isinstance(client, HealthzArtifactClient):
            raise TypeError(
                "create_artifact_client must return HealthzArtifactClient, got {}".format(
                    type(client).__name__
                )
            )
        return client

    def start(self) -> None:
        self.config = self._load_config()
        telemetry = TelemetryPublisher(
            self.state_db, self.config, serial_resolver=self._component_serial
        )
        self.telemetry = telemetry
        manager = RuleGenerationManager(
            self.paths,
            self._validate_candidate,
            self.extensions.identity.generation_identity,
        )
        try:
            activation = manager.activate()
            self.activation = activation
        except NoRulesAvailable:
            # The watcher restarts DLDD when the first rules file arrives.
            LOGGER.info("no DLDD rules source is present; stopping cleanly")
            self.stop_event.set()
            return
        except Exception as error:
            LOGGER.exception("DLDD activation failed")
            self._fail_start(str(error))
            return

        payload = activation.payload
        if isinstance(payload, ActivationPreflightResult):
            validation = payload.validation
            bundle = payload.plan_for_generation(activation.checksum)
        else:
            validation = payload
            bundle = build_plans(
                validation.materialized_rules,
                activation.checksum,
                self.config.polling_intervals,
            )
        adapters = self._adapters()
        if not bundle.work_items and not bundle.templates:
            self._fail_start("zero usable monitor work items after activation")
            return

        evidence_queue = Queue(maxsize=4096)
        try:
            artifact_client = self._create_artifact_client()
        except Exception as error:
            LOGGER.exception("DLDD artifact client initialization failed")
            self._fail_start(
                "artifact client initialization failed: {}".format(error)
            )
            return
        self.adapters = adapters
        self.evidence_queue = evidence_queue
        self.action_runner = ActionRunner(
            ActionExecutor(hooks=self.extensions.vendor_hooks)
        )
        if any(
            item.async_collection for item in bundle.work_items.values()
        ) or any(
            template.item.async_collection
            or any(item.async_collection for item in template.common_items)
            for template in bundle.templates.values()
        ):
            self.async_collection_pool = AsyncCollectionPool()
        self.artifact_client = artifact_client
        correlation = CorrelationEngine(bundle.signatures)
        ruleset = validation.ruleset
        if ruleset is None:
            raise RuntimeError("validated ruleset is unavailable")
        orchestrator = PrimaryOrchestrator(
            evidence_queue,
            bundle.monitor_plans,
            bundle.work_items,
            correlation,
            telemetry,
            self.config,
            activation.checksum,
            action_runner=self.action_runner,
            artifact_client=artifact_client,
            local_action_default_timeout=ruleset.local_action_default_timeout,
            source_lifecycle_probe=self._source_is_in_expected_maintenance,
        )
        self.orchestrator = orchestrator
        orchestrator.broken_rules.update(
            {
                item.get("correlation_key", "ingestion:{}".format(index)): item
                for index, item in enumerate(
                    tuple(activation.broken_rules)
                )
            }
        )
        persisted = self.state_store.load(
            activation.checksum, allow_crash_recovery=True
        )
        if persisted.get("recovery_error"):
            orchestrator.service_diagnostics.append(
                {
                    "reason": "broken_rule_state_not_restored",
                    "error": persisted["recovery_error"],
                    "observed_at": time.time(),
                }
            )
        for record in persisted.get("broken_rules", ()):
            key = record.get("correlation_key")
            if key in bundle.work_items and record.get("state") == "BROKEN":
                orchestrator.restore_broken_work(key, record)
        if not self._reconcile_existing_faults_at_startup():
            return
        for plan in bundle.monitor_plans.values():
            monitor = self._new_monitor(plan)
            monitor.start()
            self.monitors.append(monitor)
        config_thread = threading.Thread(
            target=self._listen_for_config,
            name="dldd-config",
            daemon=True,
        )
        self.config_thread = config_thread
        config_thread.start()
        self._persist_state_if_changed(force=True)

    def _reconcile_existing_faults_at_startup(self) -> bool:
        """Build fault state from one complete STATE_DB snapshot before polling."""

        orchestrator = self.orchestrator
        if orchestrator is None:
            raise RuntimeError("orchestrator is unavailable")
        attempt = 0
        while True:
            attempt += 1
            try:
                orchestrator.reconcile_existing_faults()
                return True
            except Exception as error:
                LOGGER.error(
                    "startup FAULT_INFO reconciliation failed (%s/%s): %s",
                    attempt,
                    TELEMETRY_FAILURE_LIMIT,
                    error,
                )
                if attempt == TELEMETRY_FAILURE_LIMIT:
                    raise TelemetryUnavailable(
                        "STATE_DB fault reconciliation failed {} consecutive "
                        "times".format(TELEMETRY_FAILURE_LIMIT)
                    ) from error
                if self.stop_event.wait(TELEMETRY_RETRY_INTERVAL):
                    return False

    def _run_artifact_query(self, query):
        executor = query.get("executor")
        if callable(executor) or query.get("type") == "cli":
            return FilesystemArtifactClient._run_query(query)
        return self.extensions.vendor_hooks.get(
            operation_hook_name(query)
        ).collect_query(query)

    def _component_serial(self, component_type: str, component_name: str) -> str:
        cache_key = (component_type, component_name)
        if cache_key in self._serial_cache:
            return self._serial_cache[cache_key]
        hook = self.extensions.vendor_hooks.get_optional("component_metadata")
        if hook is None:
            return ""
        result = hook.collect(
            {
                "operation": "get_serial_number",
                "component_type": component_type,
                "component_name": component_name,
            }
        )
        if isinstance(result, Mapping):
            serial = str(result.get("serial_number", ""))
        else:
            serial = str(result or "")
        self._serial_cache[cache_key] = serial
        return serial

    def _source_is_in_expected_maintenance(self, item) -> bool:
        hook = self.extensions.vendor_hooks.get_optional("source_lifecycle")
        if hook is None:
            return False
        result = hook.collect(
            {
                "operation": "is_expected_maintenance",
                "source_id": item.source_id,
                "source_type": item.source_type,
                "source": dict(item.source),
                "component_name": item.component_name,
            }
        )
        if isinstance(result, Mapping):
            return bool(result.get("graceful", result.get("suspended", False)))
        return bool(result)

    def _listen_for_config(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.config_provider.listen(self._apply_config)
            except Exception as error:
                if not self.stop_event.is_set():
                    LOGGER.error("DLDD_CONFIG subscription failed: %s", error)
            self.config_provider.reset()
            self.stop_event.wait(5)

    def _apply_config(self, config_db: Mapping[str, str]) -> None:
        try:
            defaults = load_vendor_defaults(self.paths.defaults)
            updated = DLDDConfig.from_sources(config_db, defaults)
        except Exception as error:
            LOGGER.error("ignoring invalid DLDD_CONFIG update: %s", error)
            return
        self.config = updated
        if self.telemetry is not None:
            self.telemetry.config = updated
        if self.orchestrator is not None:
            self.orchestrator.queue_config_update(updated)
        # Iterate a stable snapshot while supervision may replace monitors.
        intervals = updated.polling_intervals
        for monitor in tuple(self.monitors):
            monitor.update_polling_intervals(intervals)
            monitor.fault_evidence_ack_timeout = updated.fault_evidence_ack_timeout
            monitor.source_recovery_samples = updated.source_recovery_samples

    def run(self) -> None:
        telemetry_failures = 0
        next_heartbeat = time.monotonic()
        clean_shutdown = False
        try:
            self.start()
            while not self.stop_event.is_set():
                if self.orchestrator is not None:
                    processed = self.orchestrator.process_batch()
                    self._supervise_monitors()
                    if processed:
                        self._persist_state_if_changed()
                else:
                    processed = 0
                now = time.monotonic()
                if now >= next_heartbeat:
                    if self._publish_status():
                        telemetry_failures = 0
                        next_heartbeat = now + 30
                    else:
                        telemetry_failures += 1
                        next_heartbeat = now + TELEMETRY_RETRY_INTERVAL
                        if telemetry_failures >= TELEMETRY_FAILURE_LIMIT:
                            raise TelemetryUnavailable(
                                "STATE_DB telemetry publication failed {} "
                                "consecutive times".format(
                                    TELEMETRY_FAILURE_LIMIT
                                )
                            )
                self.stop_event.wait(0 if processed else 0.2)
            clean_shutdown = True
        finally:
            self.shutdown(clean_shutdown=clean_shutdown)

    def _supervise_monitors(self) -> None:
        if self.adapters is None or self.evidence_queue is None:
            return
        for monitor in tuple(self.monitors):
            if monitor.is_alive() or self.stop_event.is_set():
                continue
            LOGGER.error("restarting stopped DLDD monitor %s", monitor.plan.monitor_id)
            replacement = self._new_monitor(monitor.plan)
            replacement.diagnostics.extend(monitor.diagnostics)
            replacement.diagnostics.append(
                {
                    "monitor": monitor.plan.monitor_id,
                    "reason": "monitor thread stopped unexpectedly and was restarted",
                    "observed_at": time.time(),
                }
            )
            self.monitors.remove(monitor)
            self.monitors.append(replacement)
            replacement.start()

    def _persist_state_if_changed(self, force: bool = False) -> None:
        if self.activation is None or self.orchestrator is None:
            return
        rules = [
            record
            for record in self.orchestrator.broken_rules.values()
            if record.get("state") == "BROKEN"
        ]
        fingerprint = json.dumps(rules, sort_keys=True, default=str)
        if not force and fingerprint == self._state_fingerprint:
            return
        self.state_store.save(
            self.activation.checksum,
            rules,
            clean_shutdown=False,
        )
        self._state_fingerprint = fingerprint

    def _publish_status(self) -> bool:
        if self.telemetry is None:
            return False
        if self.activation is None:
            return self.telemetry.publish_status(
                "BROKEN|FATAL",
                "",
                "",
                "",
                reason=self.fatal_reason or "no active rules generation",
            )
        if self.fatal_reason:
            broken = _operator_status_records(
                self.startup_broken or tuple(self.activation.broken_rules)
            )
            return self.telemetry.publish_status(
                "BROKEN|FATAL",
                self.activation.schema_version,
                self.activation.active_file,
                self.activation.checksum,
                broken_rules=broken,
                reason=self.fatal_reason,
                active_rules_source=self.activation.source,
                activation_result=self.activation.validation_result,
            )
        broken = tuple(self.activation.broken_rules)
        source = ()
        work_items = {}
        state = "OK"
        active_fault_count = 0
        inflight_count = 0
        if self.orchestrator is not None:
            work_items = getattr(self.orchestrator, "work_items", {})
            broken = tuple(self.orchestrator.broken_rules.values())
            source = self.orchestrator.source_status_snapshot()
            state = self.orchestrator.service_state()
            active_fault_count = sum(
                record.status == "ACTIVE"
                for record in self.orchestrator.faults.values()
            )
            inflight_count = len(self.orchestrator.pending) + len(
                self.orchestrator.reconciliation
            )
        broken = _operator_status_records(broken, work_items)
        payload = getattr(self.activation, "payload", None)
        return self.telemetry.publish_status(
            state,
            self.activation.schema_version,
            self.activation.active_file,
            self.activation.checksum,
            rule_count=len(getattr(payload, "materialized_rules", ())),
            active_fault_count=active_fault_count,
            broken_rules=broken,
            source_status=source,
            inflight_count=inflight_count,
            reason="" if state == "OK" else "DLDD has degraded or broken rules/sources",
            active_rules_source=self.activation.source,
            activation_result=self.activation.validation_result,
        )

    def shutdown(self, clean_shutdown: bool = True) -> None:
        self.stop_event.set()
        for monitor in self.monitors:
            monitor.join(timeout=5)
        if self.action_runner is not None:
            self.action_runner.shutdown(wait=False)
        if self.async_collection_pool is not None:
            self.async_collection_pool.shutdown(wait=False)
        if self.artifact_client is not None:
            self.artifact_client.shutdown(wait=False)
        if self.activation is not None and self.orchestrator is not None:
            self.state_store.save(
                self.activation.checksum,
                (
                    record
                    for record in self.orchestrator.broken_rules.values()
                    if record.get("state") == "BROKEN"
                ),
                clean_shutdown=clean_shutdown,
            )


def run_service() -> None:
    """Run the DLDD service with signal-driven graceful shutdown."""

    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    DLDDService(stop_event=stop_event).run()
