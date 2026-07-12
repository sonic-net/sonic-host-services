from __future__ import absolute_import

from copy import deepcopy
import json
from pathlib import Path
from threading import Event, RLock, Thread
import time

import pytest
import yaml

from dldd.adapters import adapter_map
from dldd.artifacts import ArtifactRequest, HealthzArtifactClient
from dldd.dse import (
    DSEBinding,
    DSEEvaluationHandle,
    DSEExpansionPolicy,
    DSEExpansionResult,
    DSEHook,
    DSERegistry,
    DSESourceHandle,
    ResolvedEvaluation,
)
from dldd.hooks import VendorHookRegistry
from dldd.lifecycle import RulePaths
from dldd.models import ValueConfig
from dldd.platform import PlatformExtensions, PlatformIdentity
from dldd.service import DLDDService
from dldd.validation import ExactCompatibilityMatcher
from tests.dldd_fakes import FakeStateDB


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FAULT_KEY = "FAULT_INFO|TEST_SENSOR|SYMPTOM_OVER_THRESHOLD"
SOURCE_KEY = "DLDD_TEST_SENSOR|SENSOR0"
DSE_FAULT_KEY = "FAULT_INFO|DSE_SENSOR0|SYMPTOM_OVER_THRESHOLD"

CONFIG_VALUES = {
    "redis_monitor_polling_interval": "1",
    "file_monitor_polling_interval": "1",
    "common_monitor_polling_interval": "1",
    "source_unavailable_grace_period": "0",
    "individual_max_failure_threshold": "10",
    "source_recovery_samples": "1",
    "fault_evidence_ack_timeout": "2",
    "active_fault_recheck_interval": "1",
    "rules_inbox_settle_time": "1",
}


class ControlledHashSource(object):
    """Thread-safe Redis-hash source with explicit runtime failure control."""

    def __init__(self, key, values):
        self._rows = {key: dict(values)}
        self._error = None
        self._one_shot_error = None
        self._lock = RLock()
        self.read_calls = []

    def read(self, database, table, key):
        assert database == "STATE_DB"
        assert table == "DLDD_TEST_SENSOR"
        with self._lock:
            self.read_calls.append((database, table, key))
            if self._one_shot_error is not None:
                error = self._one_shot_error
                self._one_shot_error = None
                raise error
            if self._error is not None:
                raise self._error
            return dict(self._rows.get(key, {}))

    def set_value(self, value, key=SOURCE_KEY):
        with self._lock:
            self._rows.setdefault(key, {})["value"] = str(value)

    def set_row(self, key, values):
        with self._lock:
            self._rows[key] = dict(values)

    def fail_with(self, error):
        with self._lock:
            self._error = error

    def fail_once_with(self, error):
        with self._lock:
            self._one_shot_error = error

    def recover(self):
        with self._lock:
            self._error = None
            self._one_shot_error = None


class ControlledDSEHook(DSEHook):
    """Authoritative two-instance DSE with live value/comparator callbacks."""

    def __init__(self):
        self._values = {"DSE_SENSOR0": "5", "DSE_SENSOR1": "6"}
        self._thresholds = {"DSE_SENSOR0": 10.0, "DSE_SENSOR1": 10.0}
        self._visible_instances = set(self._values)
        self.authoritative = True
        self.expansion_calls = 0
        self.source_calls = []
        self.comparator_calls = []
        self.direct_rows = {}
        self.direct_read_calls = []
        self._lock = RLock()

    def read(self, database, table, key):
        with self._lock:
            self.direct_read_calls.append((database, table, key))
            if key not in self.direct_rows:
                raise AssertionError(
                    "DSE-only integration rule used the direct Redis adapter"
                )
            return dict(self.direct_rows[key])

    def resolve_source(self, reference, context):
        def expand(unused_context):
            with self._lock:
                self.expansion_calls += 1
                return DSEExpansionResult(
                    tuple(
                        DSEBinding(
                            instance=instance,
                            source_id="DSE_SENSOR|{}".format(instance),
                            value_configs=ValueConfig(type="float"),
                        )
                        for instance in sorted(self._visible_instances)
                    ),
                    authoritative=self.authoritative,
                )

        def get_value(invocation):
            with self._lock:
                instance = invocation.binding.instance
                self.source_calls.append(instance)
                return self._values[instance]

        return DSESourceHandle(
            reference,
            expand,
            get_value,
            DSEExpansionPolicy(
                bootstrap_scans=1,
                bootstrap_interval=0.05,
                warmup_cycles=1,
                stable_interval=0.1,
            ),
        )

    def resolve_evaluation(self, reference, context):
        def get_comparator(invocation):
            with self._lock:
                instance = invocation.binding.instance
                self.comparator_calls.append(instance)
                return ResolvedEvaluation(
                    expected_value=self._thresholds[instance],
                    operator=">",
                    value_configs=ValueConfig(type="float"),
                )

        return DSEEvaluationHandle(reference, get_comparator)

    def set_value(self, instance, value):
        with self._lock:
            self._values[instance] = str(value)

    def set_direct_row(self, key, values):
        with self._lock:
            self.direct_rows[key] = dict(values)

    def remove(self, instance):
        with self._lock:
            self._visible_instances.discard(instance)
            self._values.pop(instance, None)
            self._thresholds.pop(instance, None)

    def omit(self, instance):
        """Omit an instance from discovery while keeping it readable."""

        with self._lock:
            self._visible_instances.discard(instance)

    def set_authoritative(self, authoritative):
        with self._lock:
            self.authoritative = bool(authoritative)


class BlockingConfigDB(object):
    """Minimal ConfigDBConnector that remains subscribed until service stop."""

    def __init__(self, stop_event, values=None):
        self.stop_event = stop_event
        self.values = dict(values or {})
        self.callback = None

    def get_table(self, table):
        assert table == "DLDD_CONFIG"
        return {"global": dict(self.values)}

    def subscribe(self, table, callback):
        assert table == "DLDD_CONFIG"
        self.callback = callback

    def listen(self):
        self.stop_event.wait(10)


class NullArtifactClient(HealthzArtifactClient):
    """No-I/O artifact boundary for rules that do not request artifacts."""

    def request(self, metadata, logs, queries):
        raise AssertionError("integration rule unexpectedly requested an artifact")

    def status(self, artifact_id):
        return ArtifactRequest(artifact_id, "FAILED", time.time())

    def shutdown(self, wait=True):
        return None


class IntegrationService(DLDDService):
    """Real service runtime with only its external source boundaries replaced."""

    def __init__(self, source, *args, **kwargs):
        self.integration_source = source
        super(IntegrationService, self).__init__(*args, **kwargs)

    def _adapters(self):
        return adapter_map(
            hooks=self.extensions.vendor_hooks,
            redis_reader=self.integration_source.read,
        )

    def _create_artifact_client(self):
        return NullArtifactClient()


class RunningService(object):
    def __init__(self, service):
        self.service = service
        self.error = None
        self.thread = Thread(target=self._run, name="dldd-integration-service")

    def _run(self):
        try:
            self.service.run()
        except Exception as error:  # surfaced to the owning test thread
            self.error = error

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=10):
        self.service.stop_event.set()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "DLDD service did not stop in time"
        if self.error is not None:
            raise self.error

    def wait_stopped(self, timeout=10):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "DLDD service did not stop in time"
        return self.error


def eventually(predicate, timeout=8, interval=0.02):
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise AssertionError("condition was not met; last value={!r}".format(last_value))


def integration_rule_document():
    with (FIXTURES / "valid-redis-rule.json").open() as stream:
        document = json.load(stream)
    signature = document["signatures"][0]["signature"]
    metadata = signature["metadata"]
    metadata.update(
        name="DLDD_INTEGRATION_THRESHOLD",
        id=9900001,
        description="Synthetic integration sensor exceeded its threshold.",
        product_ids=["TEST-PRODUCT"],
        sw_versions=["TEST-SOFTWARE"],
        component="TEST_SENSOR",
        severity="WARNING",
    )
    conditions = signature["conditions"]
    conditions["logic"] = "1"
    conditions["logic_lookback_time"] = 0
    event = conditions["events"][0]["event"]
    event.update(
        type="redis",
        path={
            "database": "STATE_DB",
            "table": "DLDD_TEST_SENSOR",
            "key": SOURCE_KEY,
            "path": "value",
        },
        evaluation={
            "type": "comparison",
            "operator": ">",
            "value": 10.0,
            "value_configs": {"type": "float", "unit": "N/A"},
        },
        sampling_interval=1,
        match_count=1,
        match_period=0,
    )
    repair = signature["actions"]["repair_actions"]
    repair.pop("local_actions", None)
    signature["actions"].pop("log_collection", None)
    return document


def dse_integration_rule_document():
    document = deepcopy(integration_rule_document())
    signature = document["signatures"][0]["signature"]
    signature["metadata"].update(
        name="DLDD_DSE_INTEGRATION_THRESHOLD",
        id=9900002,
    )
    event = signature["conditions"]["events"][0]["event"]
    event.update(
        type="dse",
        path="{sensor*}:{get_value()}",
        evaluation={
            "type": "dse",
            "value": "{sensor*}:{get_high_threshold()}",
        },
    )
    return document


def _make_environment(
    tmp_path,
    document,
    source,
    dse_registry,
    *,
    config_values=None,
    vendor_hooks=None,
):
    platform_dir = tmp_path / "platform"
    rules_dir = tmp_path / "runtime" / "rules"
    inbox = tmp_path / "runtime" / "inbox" / "dld_rules.yaml"
    state_file = tmp_path / "runtime" / "dld_state.json"
    platform_dir.mkdir(parents=True)
    if document is not None:
        (platform_dir / "dld_rules.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
    paths = RulePaths(
        platform_dir=str(platform_dir),
        inbox=str(inbox),
        rules_dir=str(rules_dir),
        state_file=str(state_file),
    )
    state_db = FakeStateDB()
    extensions = PlatformExtensions(
        PlatformIdentity(
            "test-platform", "TEST-PRODUCT", "TEST-SOFTWARE"
        ),
        dse_registry,
        vendor_hooks or VendorHookRegistry(),
        ExactCompatibilityMatcher(),
    )

    config_dbs = []

    def service():
        stop_event = Event()
        config_db = BlockingConfigDB(
            stop_event, config_values or CONFIG_VALUES
        )
        config_dbs.append(config_db)
        return IntegrationService(
            source,
            paths=paths,
            config_db=config_db,
            state_db=state_db,
            extensions=extensions,
            stop_event=stop_event,
        )

    return {
        "paths": paths,
        "source": source,
        "state_db": state_db,
        "service": service,
        "config_dbs": config_dbs,
    }


@pytest.fixture
def integration_environment(tmp_path):
    return _make_environment(
        tmp_path,
        integration_rule_document(),
        ControlledHashSource(SOURCE_KEY, {"value": "5"}),
        DSERegistry(),
    )


@pytest.fixture
def dse_integration_environment(tmp_path):
    hook = ControlledDSEHook()
    return _make_environment(
        tmp_path,
        dse_integration_rule_document(),
        hook,
        DSERegistry(hook=hook),
    )


@pytest.fixture
def integration_environment_factory(tmp_path):
    def create(
        document=None,
        source=None,
        dse_registry=None,
        *,
        config_values=None,
        vendor_hooks=None,
    ):
        return _make_environment(
            tmp_path,
            document,
            source
            or ControlledHashSource(SOURCE_KEY, {"value": "5"}),
            dse_registry or DSERegistry(),
            config_values=config_values,
            vendor_hooks=vendor_hooks,
        )

    return create
