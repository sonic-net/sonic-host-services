"""DLDD process composition and primary service loop."""

from __future__ import annotations

import logging
import json
from dataclasses import replace
import os
import signal
import threading
import time
from queue import Queue
from typing import Mapping, Optional

from .actions import ActionExecutor, ActionRunner
from .adapters import VendorAdapter, adapter_map
from .artifacts import (
    DEFAULT_ARTIFACT_DIRECTORY,
    FilesystemArtifactClient,
    HealthzArtifactClient,
)
from .config import ConfigDBProvider, DLDDConfig, load_vendor_defaults
from .correlation import CorrelationEngine
from .lifecycle import (
    ActivationResult,
    BrokenRuleStateStore,
    CandidateValidation,
    RuleGenerationManager,
    RulePaths,
)
from .monitor import AsyncCollectionPool, MonitorThread
from .models import BrokenRule, ValidationIssue
from .hooks import VendorHookError
from .orchestrator import PrimaryOrchestrator
from .planner import build_plans
from .platform import PlatformExtensions, detect_identity, load_extensions
from .runtime import MonitorCommandType, MonitorWorkState
from .rule_schema.errors import bound_diagnostic, bound_identity
from .rule_status import build_rule_status_snapshot
from .telemetry import SonicStateDB, TelemetryPublisher
from .timestamps import floor_timestamp
from .validation import (
    MAX_SERIALIZED_DIAGNOSTIC_BYTES,
    ValidationContext,
    load_rules,
    source_line_for_path,
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
    """Return the HLD category prefix plus the original rule diagnostics."""

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

    # A fixed reason is the final fallback and preserves every broken rule
    # identity/count.  Identity projection above is JSON-safe, so the schema's
    # 1024-signature bound guarantees this representation fits the reserve.
    for record in records:
        record["reason"] = "validation details omitted by candidate byte cap"
    return tuple(records)


def _bounded_file_error_strings(issues):
    selected = [bound_diagnostic(str(issue), 1024) for issue in issues[:256]]
    if len(issues) > len(selected):
        selected.append("additional file diagnostics were omitted")
    return tuple(selected)


def validate_runtime_operation_hooks(materialized_rule, vendor_hooks) -> None:
    """Ensure every materialized non-built-in operation has a runtime target."""

    actions = materialized_rule.signature.actions
    local = actions.repair_actions.local_actions
    if local is not None:
        for operation in local.action_list:
            if operation.type == "i2c":
                vendor_hooks.validate_i2c_source(operation.path)
                continue
            if callable(operation.executor) or operation.type == "cli":
                continue
            hook_name = str(operation.options.get("hook", operation.type))
            vendor_hooks.get(hook_name)
    if actions.log_collection is not None:
        for query in actions.log_collection.queries:
            if callable(query.executor) or query.type == "cli":
                continue
            hook_name = str(query.options.get("hook", query.type))
            vendor_hooks.get(hook_name)


class DLDDService:
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
        self.telemetry = None
        self.activation = None
        self.monitors = []
        self.orchestrator = None
        self.action_runner = None
        self.async_collection_pool = None
        self.artifact_client = None
        self.adapters = None
        self.evidence_queue = None
        self.state_store = BrokenRuleStateStore(self.paths.state_file)
        self.config_thread = None
        self._state_fingerprint = None
        self.fatal_reason = ""
        self.startup_broken = ()
        self._serial_cache = {}

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
        if result.materialized_rules:
            invalid = {}
            for rule in result.materialized_rules:
                try:
                    validate_runtime_operation_hooks(
                        rule, self.extensions.vendor_hooks
                    )
                except (ValueError, VendorHookError) as error:
                    metadata = rule.signature.metadata
                    invalid.setdefault(
                        metadata.id,
                        (
                            bound_identity(metadata.name, 128),
                            bound_diagnostic(str(error), 256),
                        ),
                    )
            validation_bundle = build_plans(
                result.materialized_rules,
                "validation",
                {"redis": 60, "file": 60, "common": 60},
            )
            adapters = self._adapters()
            for item in validation_bundle.work_items.values():
                try:
                    adapters[item.source_type].validate(item)
                except (ValueError, VendorHookError) as error:
                    invalid.setdefault(
                        item.rule_id,
                        (
                            bound_identity(item.rule_name, 128),
                            bound_diagnostic(str(error), 256),
                        ),
                    )
            if invalid:
                materialized = tuple(
                    rule
                    for rule in result.materialized_rules
                    if rule.signature.metadata.id not in invalid
                )
                added_broken = tuple(
                    BrokenRule(
                        rule_name=name,
                        rule_id=rule_id,
                        rule_version=bound_identity(
                            next(
                                rule.signature.metadata.version
                                for rule in result.materialized_rules
                                if rule.signature.metadata.id == rule_id
                            ),
                            64,
                        ),
                        issues=(
                            ValidationIssue(
                                scope="rule",
                                code="activation_preflight_failed",
                                message=reason,
                                path="$.signatures",
                                rule_name=name,
                                rule_id=rule_id,
                                line=source_line_for_path(
                                    result.source_lines, "$.signatures"
                                ),
                            ),
                        ),
                    )
                    for rule_id, (name, reason) in invalid.items()
                )
                result = replace(
                    result,
                    materialized_rules=materialized,
                    broken_rules=result.broken_rules + added_broken,
                )
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
            payload=result,
        )

    def _adapters(self):
        adapters = adapter_map(hooks=self.extensions.vendor_hooks)
        for source_type in self.extensions.dse_registry.source_types:
            adapters[source_type] = VendorAdapter(
                source_type, self.extensions.vendor_hooks
            )
        return adapters

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
        self.telemetry = TelemetryPublisher(
            self.state_db, self.config, serial_resolver=self._component_serial
        )
        manager = RuleGenerationManager(
            self.paths,
            self._validate_candidate,
            self.extensions.identity.generation_identity,
        )
        try:
            self.activation = manager.activate()
        except Exception as error:
            LOGGER.exception("DLDD activation failed")
            self.fatal_reason = str(error)
            self.telemetry.publish_status(
                "BROKEN|FATAL", "", "", "", reason=str(error)
            )
            return

        validation = self.activation.payload
        intervals = {
            "redis": self.config.redis_monitor_polling_interval,
            "file": self.config.file_monitor_polling_interval,
            "common": self.config.common_monitor_polling_interval,
        }
        bundle = build_plans(
            validation.materialized_rules,
            self.activation.checksum,
            intervals,
        )
        adapters = self._adapters()
        if not bundle.work_items:
            self.fatal_reason = "zero usable monitor work items after activation"
            self.startup_broken = tuple(self.activation.broken_rules)
            self.telemetry.publish_status(
                "BROKEN|FATAL",
                self.activation.schema_version,
                self.activation.active_file,
                self.activation.checksum,
                broken_rules=self.startup_broken,
                reason=self.fatal_reason,
            )
            return

        evidence_queue = Queue(maxsize=4096)
        try:
            artifact_client = self._create_artifact_client()
        except Exception as error:
            LOGGER.exception("DLDD artifact client initialization failed")
            self.fatal_reason = "artifact client initialization failed: {}".format(
                error
            )
            self.startup_broken = tuple(self.activation.broken_rules)
            self.telemetry.publish_status(
                "BROKEN|FATAL",
                self.activation.schema_version,
                self.activation.active_file,
                self.activation.checksum,
                broken_rules=self.startup_broken,
                reason=self.fatal_reason,
            )
            return
        self.adapters = adapters
        self.evidence_queue = evidence_queue
        self.action_runner = ActionRunner(
            ActionExecutor(hooks=self.extensions.vendor_hooks)
        )
        if any(item.async_collection for item in bundle.work_items.values()):
            self.async_collection_pool = AsyncCollectionPool()
        self.artifact_client = artifact_client
        correlation = CorrelationEngine(bundle.signatures)
        self.orchestrator = PrimaryOrchestrator(
            evidence_queue,
            bundle.monitor_plans,
            bundle.work_items,
            correlation,
            self.telemetry,
            self.config,
            self.activation.checksum,
            action_runner=self.action_runner,
            artifact_client=artifact_client,
            local_action_default_timeout=validation.ruleset.local_action_default_timeout,
            source_lifecycle_probe=self._source_is_in_expected_maintenance,
        )
        self.orchestrator.broken_rules.update(
            {
                item.get("correlation_key", "ingestion:{}".format(index)): item
                for index, item in enumerate(
                    tuple(self.activation.broken_rules)
                )
            }
        )
        persisted = self.state_store.load(
            self.activation.checksum, allow_crash_recovery=True
        )
        if persisted.get("recovery_error"):
            self.orchestrator.service_diagnostics.append(
                {
                    "reason": "broken_rule_state_not_restored",
                    "error": persisted["recovery_error"],
                    "observed_at": time.time(),
                }
            )
        for record in persisted.get("broken_rules", ()):
            key = record.get("correlation_key")
            if key in bundle.work_items and record.get("state") == "BROKEN":
                self.orchestrator.broken_rules[key] = record
                self.orchestrator._command_key(
                    key,
                    MonitorCommandType.SUSPEND,
                    MonitorWorkState.BROKEN,
                    "restored broken rule after unclean restart",
                )
        self.orchestrator.reconcile_existing_faults()
        for plan in bundle.monitor_plans.values():
            monitor = MonitorThread(
                plan,
                adapters,
                evidence_queue,
                fault_evidence_ack_timeout=self.config.fault_evidence_ack_timeout,
                source_recovery_samples=self.config.source_recovery_samples,
                async_collection_pool=self.async_collection_pool,
                stop_event=self.stop_event,
            )
            monitor.start()
            self.monitors.append(monitor)
        self.config_thread = threading.Thread(
            target=self._listen_for_config,
            name="dldd-config",
            daemon=True,
        )
        self.config_thread.start()
        self._persist_state_if_changed(force=True)
        self._publish_status()

    def _run_artifact_query(self, query):
        executor = query.get("executor")
        if callable(executor) or query.get("type") == "cli":
            return FilesystemArtifactClient._run_query(query)
        hook_name = str(query.get("hook", query.get("type", "")))
        return self.extensions.vendor_hooks.get(hook_name).collect_query(query)

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
            if hasattr(self.orchestrator, "queue_config_update"):
                self.orchestrator.queue_config_update(updated)
            else:  # lightweight test doubles
                self.orchestrator.config = updated
        # Monitor supervision may replace a stopped thread concurrently.  A
        # stable snapshot prevents list compaction from skipping another plan;
        # replacements reuse the same plan-owned update queue.
        for monitor in tuple(self.monitors):
            if monitor.plan.monitor_type == "redis":
                interval = updated.redis_monitor_polling_interval
            elif monitor.plan.monitor_type == "file":
                interval = updated.file_monitor_polling_interval
            else:
                interval = updated.common_monitor_polling_interval
            if hasattr(monitor, "update_polling_interval"):
                monitor.update_polling_interval(interval)
            else:  # lightweight test doubles
                monitor.plan.polling_interval = interval
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
            replacement = MonitorThread(
                monitor.plan,
                self.adapters,
                self.evidence_queue,
                fault_evidence_ack_timeout=self.config.fault_evidence_ack_timeout,
                source_recovery_samples=self.config.source_recovery_samples,
                async_collection_pool=self.async_collection_pool,
                stop_event=self.stop_event,
            )
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

    def _rule_status_snapshot(self):
        """Aggregate the active generation into bounded operator-facing rows."""
        return build_rule_status_snapshot(
            self.activation,
            self.orchestrator,
            self.monitors,
        )

    def _publish_rule_status(self) -> bool:
        if self.telemetry is None:
            return False
        if self.activation is None:
            return self.telemetry.clear_rule_status()
        try:
            rules, detail_truncated = self._rule_status_snapshot()
        except Exception:
            LOGGER.exception("unable to build DLDD rule status snapshot")
            return False
        return self.telemetry.publish_rule_status(
            self.activation.checksum,
            rules,
            detail_truncated=detail_truncated,
        )

    def _publish_status(self) -> bool:
        if self.telemetry is None:
            return False
        if self.activation is None:
            rule_status_published = self._publish_rule_status()
            status_published = self.telemetry.publish_status(
                "BROKEN|FATAL",
                "",
                "",
                "",
                reason=self.fatal_reason or "no active rules generation",
            )
            return status_published and rule_status_published
        if self.fatal_reason:
            rule_status_published = self._publish_rule_status()
            status_published = self.telemetry.publish_status(
                "BROKEN|FATAL",
                self.activation.schema_version,
                self.activation.active_file,
                self.activation.checksum,
                broken_rules=(
                    self.startup_broken
                    or tuple(self.activation.broken_rules)
                ),
                reason=self.fatal_reason,
                local_action_default_timeout=(
                    self.activation.payload.ruleset.local_action_default_timeout
                    if self.activation.payload.ruleset
                    else None
                ),
                active_rules_source=self.activation.source,
                activation_result=self.activation.validation_result,
                activation_fallback_used=self.activation.fallback_used,
                previous_active_rules_checksum=self.activation.previous_checksum,
            )
            return status_published and rule_status_published
        broken = tuple(self.activation.broken_rules)
        source = ()
        inflight = ()
        diagnostics = ()
        state = "OK"
        if self.orchestrator is not None:
            recovered_cutoff = time.time() - 30
            for source_id, source_record in list(
                self.orchestrator.source_status.items()
            ):
                if (
                    source_record.get("state") == "RECOVERED"
                    and source_record.get("since", 0) < recovered_cutoff
                ):
                    self.orchestrator.source_status.pop(source_id, None)
            broken = tuple(self.orchestrator.broken_rules.values())
            source = tuple(self.orchestrator.source_status.values())
            inflight = self._inflight_status()
            state = self.orchestrator.service_state()
            diagnostics = tuple(
                diagnostic
                for monitor in self.monitors
                for diagnostic in monitor.diagnostics
            ) + tuple(self.orchestrator.service_diagnostics) + tuple(
                self.orchestrator.correlation.diagnostics
            )
        status_published = self.telemetry.publish_status(
            state,
            self.activation.schema_version,
            self.activation.active_file,
            self.activation.checksum,
            broken_rules=broken,
            source_status=source,
            inflight_fault_evidence=inflight,
            service_diagnostics=diagnostics,
            reason="" if state == "OK" else "DLDD has degraded or broken rules/sources",
            local_action_default_timeout=(
                self.activation.payload.ruleset.local_action_default_timeout
                if self.activation.payload.ruleset
                else None
            ),
            active_rules_source=self.activation.source,
            activation_result=self.activation.validation_result,
            activation_fallback_used=self.activation.fallback_used,
            previous_active_rules_checksum=self.activation.previous_checksum,
        )
        return status_published and self._publish_rule_status()

    def _inflight_status(self):
        """Return durable primary-owned work, not transient queue handoffs.

        Process status is refreshed every 30 seconds.  Publishing a normal
        COLLECTING or IN_FLIGHT handoff can therefore make a sub-second state
        look stuck for an entire heartbeat interval.  Lease failures already
        surface through service diagnostics, so this operator view is limited
        to intentional holds and requested rechecks.
        """

        result = []
        monotonic_now = time.monotonic()
        wall_now = time.time()
        for monitor in self.monitors:
            for key, state in monitor.plan.state_by_key.items():
                if state.state.value not in (
                    "HELD_BY_PRIMARY",
                    "RECHECK_REQUESTED",
                ):
                    continue
                item = monitor.plan.items_by_key[key]
                status = {
                    "correlation_key": key,
                    "rule": item.rule_name,
                    "rule_id": item.rule_id,
                    "event_id": item.event_id,
                    "component": item.component_name,
                    "state": state.state.value,
                    "reason": "primary_owned",
                    "since": state.last_enqueue_timestamp,
                    "hold_deadline": (
                        wall_now + (state.hold_deadline - monotonic_now)
                        if state.hold_deadline is not None
                        else None
                    ),
                    "owning_monitor": monitor.plan.monitor_id,
                }
                pending = next(
                    (
                        item
                        for item in self.orchestrator.pending.values()
                        if key in self.orchestrator._execution_keys(item.execution)
                    ),
                    None,
                )
                if pending is not None:
                    action_result = pending.action_result
                    wait_until = None
                    if pending.wait_until is not None:
                        wait_until = wall_now + (pending.wait_until - monotonic_now)
                    status["local_action_state"] = {
                        "state": (
                            "RUNNING"
                            if pending.phase == "ACTIONS"
                            else "WAITING_FOR_RECHECK"
                        ),
                        "worker_id": (
                            action_result.worker_id
                            if action_result is not None
                            else getattr(pending.future, "dldd_worker_id", "")
                        ),
                        "started_at": pending.first_decision.event.event_timestamp,
                        "wait_until": wait_until,
                        "last_error": (
                            action_result.last_error
                            if action_result is not None
                            else ""
                        ),
                    }
                result.append(status)
        return tuple(result)

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
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    DLDDService(stop_event=stop_event).run()
