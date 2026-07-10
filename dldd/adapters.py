"""Data-source adapters used by monitor threads."""

from __future__ import annotations

import glob
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .dse import (
    DSEBinding,
    DSEExpansionResult,
    DSEInvocationContext,
    validate_resolved_evaluation,
)
from .command_execution import build_i2c_argv, run_shell_free
from .evaluators import EvaluationContractError, evaluate, parse_integer
from .hooks import VendorHookRegistry
from .models import ValueConfig
from .runtime import (
    CollectedValue,
    EvaluationResult,
    EvaluationResultType,
    MonitorWorkItem,
    SourceAvailability,
)
from .sonic_hash import SonicHashReader, SonicHashReaderError


class AdapterError(RuntimeError):
    pass


class SourceUnavailable(AdapterError):
    pass


def _extract_path(value: Any, path: Any) -> Any:
    if path in (None, "", []):
        return value
    if isinstance(path, (list, tuple)):
        parts = path
    else:
        normalized = str(path).lstrip("$").lstrip("./")
        parts = normalized.split("/") if "/" in normalized else normalized.split(".")
    current = value
    for part in parts:
        if isinstance(current, str):
            stripped = current.strip()
            if stripped.startswith(("{", "[")):
                current = json.loads(stripped)
        if isinstance(current, Mapping):
            current = current[str(part)]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            current = current[int(part)]
        else:
            raise KeyError("cannot traverse path component {!r}".format(part))
    return current


def _normalize(raw: Any, config: ValueConfig) -> Any:
    value_type = config.type
    if value_type == "N/A":
        value = raw
    elif value_type == "string":
        if isinstance(raw, bytes):
            encoding = config.encoding if config.encoding != "N/A" else "utf-8"
            value = raw.decode(encoding, "replace")
        else:
            value = str(raw)
    elif value_type in ("binary", "hex", "int"):
        value = parse_integer(raw)
    elif value_type == "float":
        value = float(raw)
    elif value_type == "boolean":
        if isinstance(raw, bool):
            value = raw
        elif str(raw).strip().lower() in ("true", "1", "yes", "on"):
            value = True
        elif str(raw).strip().lower() in ("false", "0", "no", "off"):
            value = False
        else:
            raise ValueError("invalid boolean value")
    elif value_type == "json":
        value = json.loads(raw) if isinstance(raw, str) else raw
    elif value_type == "bytes":
        encoding = config.encoding if config.encoding != "N/A" else "utf-8"
        value = raw if isinstance(raw, bytes) else str(raw).encode(encoding)
    else:
        raise ValueError("unsupported value type: {}".format(value_type))

    if config.scaling not in (None, "", "N/A"):
        value = value * float(config.scaling)
    return value


def _condition_config(evaluator: Mapping[str, Any]) -> ValueConfig:
    return ValueConfig.from_mapping(evaluator.get("value_configs") or {})


class DataSourceAdapter(ABC):
    source_type = ""

    def validate(self, item: MonitorWorkItem) -> None:
        if item.source_type != self.source_type:
            raise ValueError(
                "{} adapter cannot handle {}".format(self.source_type, item.source_type)
            )

    @abstractmethod
    def get_value(self, item: MonitorWorkItem) -> Any:
        raise NotImplementedError

    def get_evaluator(self, item: MonitorWorkItem) -> Mapping[str, Any]:
        handle = item.dse_evaluation_handle
        if handle is not None:
            binding = item.dse_binding or DSEBinding(
                instance=item.component_name,
                source_id=item.source_id,
                data=item.source,
            )
            rule_operator = item.evaluation.get("operator")
            resolved = validate_resolved_evaluation(
                handle.get_comparator(
                    DSEInvocationContext(item.dse_context, binding)
                ),
                rule_operator=rule_operator,
                reference=handle.reference,
            )
            rule_config = ValueConfig.from_mapping(
                item.evaluation.get("value_configs") or {}
            )
            config = (
                rule_config
                if rule_config != ValueConfig()
                else resolved.value_configs
            )
            config_values = config.as_payload()
            evaluator = {
                "type": "dse",
                "value": resolved.expected_value,
                "value_configs": config_values,
            }
            effective_operator = rule_operator or resolved.operator
            if effective_operator is not None:
                evaluator["operator"] = effective_operator
            if resolved.comparator is not None:
                evaluator["comparator"] = resolved.comparator
            return evaluator
        return item.evaluation

    def run_evaluation(self, value: CollectedValue, evaluator: Mapping[str, Any]) -> bool:
        if isinstance(value.normalized, list):
            return any(evaluate(evaluator, item) for item in value.normalized)
        return evaluate(evaluator, value.normalized)

    def collect(self, item: MonitorWorkItem) -> EvaluationResult:
        started = time.time()
        try:
            raw = self.get_value(item)
            normalized = (
                [_normalize(value, item.value_config) for value in raw]
                if isinstance(raw, list)
                else _normalize(raw, item.value_config)
            )
            value = CollectedValue(raw, normalized, item.value_config)
        except SourceUnavailable as error:
            return EvaluationResult(
                EvaluationResultType.SOURCE_UNAVAILABLE,
                source_status=SourceAvailability.UNAVAILABLE,
                collection_started_at=started,
                completed_at=time.time(),
                error_category="SOURCE_UNAVAILABLE",
                error=str(error),
                retryable=True,
            )
        except Exception as error:  # adapter boundary: normalize external failures
            return EvaluationResult(
                EvaluationResultType.COLLECTION_ERROR,
                source_status=SourceAvailability.UNAVAILABLE,
                collection_started_at=started,
                completed_at=time.time(),
                error_category="COLLECTION_ERROR",
                error=str(error),
                retryable=True,
            )

        evaluator = item.evaluation
        try:
            evaluator = self.get_evaluator(item)
            matched = self.run_evaluation(value, evaluator)
        except Exception as error:
            contract_error = isinstance(
                error, (EvaluationContractError, TypeError, ValueError)
            )
            return EvaluationResult(
                EvaluationResultType.EVALUATION_ERROR,
                value=value,
                evaluator_type=str(evaluator.get("type", "")),
                operator=str(evaluator.get("operator", evaluator.get("logic", ""))),
                expected=evaluator.get("value"),
                condition_config=_condition_config(evaluator),
                collection_started_at=started,
                completed_at=time.time(),
                error_category="EVALUATION_ERROR",
                error=str(error),
                retryable=not contract_error,
            )

        return EvaluationResult(
            EvaluationResultType.MATCH if matched else EvaluationResultType.NO_MATCH,
            value=value,
            evaluator_type=str(evaluator.get("type", "")),
            operator=str(evaluator.get("operator", evaluator.get("logic", ""))),
            expected=evaluator.get("value"),
            condition_config=_condition_config(evaluator),
            source_status=SourceAvailability.AVAILABLE,
            collection_started_at=started,
            completed_at=time.time(),
        )


class RedisAdapter(DataSourceAdapter):
    source_type = "redis"

    def __init__(
        self,
        reader: Optional[Callable[[str, str, str], Any]] = None,
        hash_reader: Optional[SonicHashReader] = None,
    ) -> None:
        if reader is not None and hash_reader is not None:
            raise ValueError("provide either reader or hash_reader")
        self._reader = reader
        self._hash_reader = hash_reader or (
            SonicHashReader() if reader is None else None
        )

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        for name in ("database", "table", "key"):
            value = item.source.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError("Redis source requires '{}'".format(name))
        path = item.source.get("path")
        if path is not None and not isinstance(path, (str, list, tuple)):
            raise ValueError("Redis source path must be a string or component list")

    def get_value(self, item: MonitorWorkItem) -> Any:
        source = item.source
        if self._reader is not None:
            value = self._reader(
                source["database"], source["table"], source["key"]
            )
        else:
            redis_key = source["key"] or source["table"]
            try:
                value = self._hash_reader.read(source["database"], redis_key)
            except SonicHashReaderError as error:
                raise SourceUnavailable(str(error))
            if not value:
                raise SourceUnavailable(
                    "Redis key is unavailable: {}".format(redis_key)
                )
        return _extract_path(value, source.get("path"))


class FileAdapter(DataSourceAdapter):
    source_type = "file"
    FORMATS = frozenset(
        ("text", "string", "raw", "json", "yaml", "integer", "int", "float", "boolean")
    )

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        if not isinstance(item.source.get("file"), str) or not item.source.get("file"):
            raise ValueError("file source requires 'file'")
        format_name = str(item.source.get("format", "text")).lower()
        if format_name not in self.FORMATS:
            raise ValueError("unsupported file format: {}".format(format_name))
        encoding = item.source.get("encoding", "utf-8")
        if not isinstance(encoding, str) or not encoding:
            raise ValueError("file source encoding must be a non-empty string")

    def get_value(self, item: MonitorWorkItem) -> Any:
        pattern = str(item.source["file"])
        paths = sorted(glob.glob(pattern))
        if not paths:
            raise SourceUnavailable("file source does not exist: {}".format(pattern))
        values = [self._read_path(path, item) for path in paths]
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _read_path(path: str, item: MonitorWorkItem) -> Any:
        with open(path, "r", encoding=item.source.get("encoding", "utf-8")) as stream:
            content = stream.read()
        format_name = str(item.source.get("format", "text")).lower()
        if format_name in ("text", "string", "raw"):
            parsed = content.strip()
        elif format_name == "json":
            parsed = json.loads(content)
        elif format_name == "yaml":
            try:
                import yaml
            except ImportError as error:
                raise AdapterError("YAML support is unavailable: {}".format(error))
            parsed = yaml.safe_load(content)
        elif format_name in ("integer", "int"):
            parsed = int(content.strip(), 0)
        elif format_name == "float":
            parsed = float(content.strip())
        elif format_name == "boolean":
            normalized = content.strip().lower()
            if normalized not in ("true", "false", "1", "0"):
                raise AdapterError("invalid boolean file value")
            parsed = normalized in ("true", "1")
        else:
            raise AdapterError("unsupported file format: {}".format(format_name))
        return _extract_path(parsed, item.source.get("path"))


class SysfsAdapter(FileAdapter):
    source_type = "sysfs"


class CLIAdapter(DataSourceAdapter):
    source_type = "cli"

    def __init__(self, runner: Optional[Callable[..., Any]] = None) -> None:
        self._runner = runner

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        argv = item.source.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(arg, str) and arg for arg in argv
        ):
            raise ValueError("CLI source requires a non-empty argv string list")
        timeout = item.source.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("CLI source timeout must be positive")
        max_output = item.source.get("max_output_bytes", 1024 * 1024)
        if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0:
            raise ValueError("CLI source max_output_bytes must be a positive integer")

    def get_value(self, item: MonitorWorkItem) -> Any:
        argv = list(item.source["argv"])
        timeout = item.source.get("timeout", 30)
        max_output = int(item.source.get("max_output_bytes", 1024 * 1024))
        result = run_shell_free(
            argv,
            timeout=timeout,
            max_output_bytes=max_output,
            runner=self._runner,
        )
        if result.returncode != 0:
            raise AdapterError(
                "CLI source exited {}: {}".format(
                    result.returncode, result.stderr_text()
                )
            )
        stdout = result.stdout_text(
            item.source.get("encoding", "utf-8"), "replace"
        )
        return _extract_path(stdout.strip(), item.source.get("path"))


class I2CAdapter(DataSourceAdapter):
    source_type = "i2c"

    def __init__(
        self,
        reader: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        hooks: Optional[VendorHookRegistry] = None,
    ) -> None:
        self._reader = reader or self._i2cget
        self._hooks = hooks or VendorHookRegistry()

    @staticmethod
    def _i2cget(source: Mapping[str, Any]) -> Any:
        result = run_shell_free(
            build_i2c_argv(source, operation="get"),
            timeout=float(source.get("timeout", 10)),
        )
        if result.returncode != 0:
            raise SourceUnavailable(result.stderr_text().strip())
        return result.stdout_text("ascii", "strict").strip()

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        if item.source.get("i2c_type") != "get":
            raise ValueError("direct I2C monitoring is read-only and requires type 'get'")
        for name in ("bus", "chip_addr", "command"):
            value = item.source.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError("I2C source requires '{}'".format(name))
        for name in ("chip_addr", "command"):
            try:
                parse_integer(item.source[name])
            except (TypeError, ValueError):
                raise ValueError("I2C source '{}' must be an integer value".format(name))
        size = item.source.get("size")
        if size not in (None, "", "N/A", "b", "w", "l"):
            raise ValueError("I2C source size must be b, w, or l")
        timeout = item.source.get("timeout", 10)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("I2C source timeout must be positive")
        self._hooks.validate_i2c_source(item.source)

    def get_value(self, item: MonitorWorkItem) -> Any:
        source = dict(item.source)
        source["bus"] = self._hooks.resolve_i2c_bus(
            source["bus"], item.source
        )
        return self._reader(source)


class PlatformAPIAdapter(DataSourceAdapter):
    source_type = "platform_api"

    def __init__(self, hooks: VendorHookRegistry) -> None:
        self._hooks = hooks

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        if not item.source.get("hook"):
            raise ValueError("platform API source requires a registered hook name")
        hook = self._hooks.get(str(item.source["hook"]))
        hook.validate_source(item.source)

    def get_value(self, item: MonitorWorkItem) -> Any:
        hook = self._hooks.get(str(item.source["hook"]))
        return hook.collect(item.source)


class VendorAdapter(PlatformAPIAdapter):
    """Adapter for a vendor-advertised source type resolved by DSE."""

    def __init__(self, source_type: str, hooks: VendorHookRegistry) -> None:
        super().__init__(hooks)
        self.source_type = source_type

    def validate(self, item: MonitorWorkItem) -> None:
        DataSourceAdapter.validate(self, item)
        hook = self._hooks.get(str(item.source.get("hook", self.source_type)))
        hook.validate_source(item.source)

    def get_value(self, item: MonitorWorkItem) -> Any:
        hook = self._hooks.get(str(item.source.get("hook", self.source_type)))
        return hook.collect(item.source)


class DSEAdapter(DataSourceAdapter):
    """Invoke already-resolved vendor handles only from monitor context."""

    source_type = "dse"

    def validate(self, item: MonitorWorkItem) -> None:
        super().validate(item)
        if item.dse_source_handle is None:
            raise ValueError("DSE source requires a resolved source handle")
        if item.dse_binding is None:
            raise ValueError("DSE source requires an expanded instance binding")

    def expand(self, template) -> DSEExpansionResult:
        result = template.source_handle.expand(template.item.dse_context)
        if not isinstance(result, DSEExpansionResult):
            raise AdapterError(
                "DSE source expansion must return DSEExpansionResult"
            )
        return result

    def get_value(self, item: MonitorWorkItem) -> Any:
        self.validate(item)
        return item.dse_source_handle.get_value(
            DSEInvocationContext(item.dse_context, item.dse_binding)
        )


def adapter_map(
    hooks: Optional[VendorHookRegistry] = None,
    redis_reader: Optional[Callable[[str, str, str], Any]] = None,
) -> Dict[str, DataSourceAdapter]:
    hooks = hooks or VendorHookRegistry()
    return {
        "redis": RedisAdapter(redis_reader),
        "file": FileAdapter(),
        "sysfs": SysfsAdapter(),
        "cli": CLIAdapter(),
        "i2c": I2CAdapter(hooks=hooks),
        "platform_api": PlatformAPIAdapter(hooks),
        "dse": DSEAdapter(),
    }
