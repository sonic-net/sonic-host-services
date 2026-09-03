"""Rules generation activation and broken-rule state persistence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple

from .filesystem import (
    atomic_copy,
    atomic_write_json,
    load_json_object,
    unlink_if_exists,
)
from .timestamps import floor_timestamp, floor_timestamp_fields


LOGGER = logging.getLogger(__name__)


class NoRulesAvailable(RuntimeError):
    """Raised when none of the configured rules generation files exists."""


@dataclass(frozen=True)
class RulePaths:
    """Filesystem locations owned by the rule-generation lifecycle."""

    platform_dir: str
    inbox: str = "/var/lib/sonic/dldd/inbox/dld_rules.yaml"
    rules_dir: str = "/var/lib/sonic/dldd/rules"
    state_file: str = "/var/lib/sonic/dld_state.json"

    @property
    def packaged(self) -> str:
        return os.path.join(self.platform_dir, "dld_rules.yaml")

    @property
    def golden(self) -> str:
        return os.path.join(self.platform_dir, "dld_rules_golden.yaml")

    @property
    def dse(self) -> str:
        return os.path.join(self.platform_dir, "dld_dse.yaml")

    @property
    def defaults(self) -> str:
        return os.path.join(self.platform_dir, "dldd-config.yaml")

    @property
    def active(self) -> str:
        return os.path.join(self.rules_dir, "dld_rules.active.yaml")

    @property
    def lock(self) -> str:
        return os.path.join(self.rules_dir, ".dldd-rules-update.lock")

    @property
    def manifest(self) -> str:
        return os.path.join(self.rules_dir, "activation.json")

    @property
    def watcher_state(self) -> str:
        return os.path.join(self.rules_dir, ".watch-state.json")


@dataclass(frozen=True)
class CandidateValidation:
    file_valid: bool
    usable_rule_count: int
    schema_version: str = ""
    broken_rules: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    errors: Tuple[str, ...] = field(default_factory=tuple)
    payload: Any = None

    @property
    def activatable(self) -> bool:
        return self.file_valid and self.usable_rule_count > 0


@dataclass(frozen=True)
class ActivationResult:
    active_file: str
    checksum: str
    source: str
    schema_version: str
    broken_rules: Tuple[Mapping[str, Any], ...]
    payload: Any
    validation_result: str = "PASSED"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:{}".format(digest.hexdigest())


def _file_identity(file_stat) -> Tuple[int, int, int, int]:
    return tuple(
        getattr(file_stat, field)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    )


class RuleGenerationManager:
    """Choose, validate, and atomically promote one rules generation."""

    MAX_ACTIVATION_ATTEMPTS = 20

    def __init__(
        self,
        paths: RulePaths,
        validator: Callable[[str, str], CandidateValidation],
        platform_identity: str,
        retention: int = 5,
        clock=time.time,
    ) -> None:
        self.paths = paths
        self.validator = validator
        self.platform_identity = platform_identity
        self.retention = max(2, retention)
        self.clock = clock

    @contextmanager
    def locked(self):
        os.makedirs(self.paths.rules_dir, mode=0o755, exist_ok=True)
        with open(self.paths.lock, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def activate(self) -> ActivationResult:
        with self.locked():
            manifest = load_json_object(self.paths.manifest)
            # Drop fields written by the retired automatic-rollback design.
            # Existing devices shed the stale operator-facing metadata on the
            # next activation attempt instead of carrying it indefinitely.
            for key in ("previous_active_checksum", "previous_active_generation_path"):
                manifest.pop(key, None)
            for attempt in manifest.get("activation_attempts", []):
                if isinstance(attempt, dict):
                    for key in (
                        "previous_checksum",
                        "fallback_used",
                        "fallback_reasons",
                        "rollback_used",
                    ):
                        attempt.pop(key, None)
            candidates = self._candidates(manifest)
            failures = []
            candidate_present = False
            for source, path in candidates:
                if not os.path.lexists(path):
                    continue
                candidate_present = True
                if not os.path.isfile(path):
                    message = "{} candidate is not a regular file: {}".format(
                        source, path
                    )
                    LOGGER.warning(message)
                    failures.append(message)
                    self._record_attempt(manifest, source, "", failure_reason=message)
                    continue
                try:
                    staged = self._stage_candidate(path)
                except (OSError, RuntimeError) as error:
                    message = "{} candidate could not be staged: {}".format(
                        source, error
                    )
                    LOGGER.warning(message)
                    failures.append(message)
                    self._record_attempt(manifest, source, "", failure_reason=message)
                    continue
                checksum = ""
                attempt = None
                try:
                    archive_failure = True
                    # Hash, validate, archive, and promote one immutable local
                    # snapshot.  In particular, never validate the mutable
                    # inbox and then copy different bytes into the active file.
                    checksum = sha256_file(staged)
                    if source == "inbox":
                        accepted_checksum = load_json_object(
                            self.paths.watcher_state
                        ).get("last_restart_checksum")
                        if checksum != accepted_checksum:
                            archive_failure = False
                            raise RuntimeError(
                                "staged inbox no longer matches the watcher-accepted generation"
                            )
                    try:
                        validation = self.validator(staged, self.paths.dse)
                    except Exception as error:
                        validation = CandidateValidation(
                            False,
                            0,
                            errors=("candidate validation failed: {}".format(error),),
                        )
                    if sha256_file(staged) != checksum:
                        raise RuntimeError(
                            "candidate changed while it was being validated"
                        )
                    attempt = self._record_attempt(
                        manifest, source, checksum, validation
                    )
                    if not validation.activatable:
                        reasons = tuple(attempt["errors"])
                        failures.extend(
                            "{} candidate rejected: {}".format(source, reason)
                            for reason in reasons
                        )
                        self._archive_failed(staged, checksum, source)
                        continue
                    generation_path = manifest.get("active_generation_path")
                    if (
                        path != self.paths.active
                        or manifest.get("active_checksum") != checksum
                    ):
                        generation_path = self._promote(staged, checksum)
                except Exception as error:
                    message = "{} candidate activation failed: {}".format(
                        source, error
                    )
                    LOGGER.warning(message)
                    failures.append(message)
                    if attempt is None:
                        attempt = self._record_attempt(
                            manifest, source, checksum, failure_reason=message
                        )
                    else:
                        attempt["activation_result"] = "FAILED"
                        errors = attempt.setdefault("errors", [])
                        if message not in errors:
                            errors.append(message)
                        attempt["reason"] = "; ".join(errors)
                        manifest["last_attempt"] = attempt
                    if archive_failure:
                        try:
                            if checksum:
                                self._archive_failed(staged, checksum, source)
                        except Exception:
                            LOGGER.exception(
                                "unable to archive failed %s candidate", source
                            )
                    continue
                finally:
                    unlink_if_exists(staged)
                validation_result = (
                    "DEGRADED" if validation.broken_rules else "PASSED"
                )
                activated_at = floor_timestamp(self.clock())
                attempt.update(
                    {
                        "activation_result": "ACTIVATED",
                        "reason": "activated",
                        "generation_path": generation_path,
                        "active_checksum": checksum,
                    }
                )
                manifest["last_attempt"] = attempt
                manifest.update(
                    {
                        "active_checksum": checksum,
                        "active_source": source,
                        "platform_identity": self.platform_identity,
                        "activated_at": activated_at,
                        "schema_version": validation.schema_version,
                        "active_generation_path": generation_path,
                        "last_activation": {
                            "at": activated_at,
                            "source": source,
                            "active_checksum": checksum,
                            "validation_result": validation_result,
                            "activation_result": "ACTIVATED",
                        },
                    }
                )
                atomic_write_json(self.paths.manifest, manifest)
                self._prune_generations(manifest)
                LOGGER.info(
                    "activated DLDD rules source=%s checksum=%s validation=%s",
                    source,
                    checksum,
                    validation_result,
                )
                return ActivationResult(
                    active_file=self.paths.active,
                    checksum=checksum,
                    source=source,
                    schema_version=validation.schema_version,
                    broken_rules=validation.broken_rules,
                    payload=validation.payload,
                    validation_result=validation_result,
                )

            if not candidate_present:
                raise NoRulesAvailable("no rules candidates exist")

            manifest["last_failure"] = {
                "at": floor_timestamp(self.clock()),
                "errors": failures or ["no rules candidates exist"],
            }
            atomic_write_json(self.paths.manifest, manifest)
            self._prune_generations(manifest)
            raise RuntimeError("no candidate produced a usable rules generation: {}".format(
                "; ".join(failures or ["no candidate exists"])
            ))

    def _stage_candidate(self, source_path: str) -> str:
        """Copy one stable candidate snapshot onto the promotion filesystem."""

        descriptor, staged = tempfile.mkstemp(
            prefix=".dld_rules.candidate.",
            suffix=".yaml",
            dir=self.paths.rules_dir,
        )
        try:
            with open(source_path, "rb") as source, os.fdopen(
                descriptor, "wb"
            ) as destination:
                descriptor = -1
                before = os.fstat(source.fileno())
                shutil.copyfileobj(source, destination, 1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
                after = os.fstat(source.fileno())
            if _file_identity(before) != _file_identity(after):
                raise RuntimeError("candidate changed while it was being staged")
            if os.path.getsize(staged) != before.st_size:
                raise RuntimeError("candidate size changed while it was being staged")
            os.chmod(staged, 0o440)
            return staged
        except Exception:
            unlink_if_exists(staged)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _candidates(self, manifest: Mapping[str, Any]) -> List[Tuple[str, str]]:
        candidates: List[Tuple[str, str]] = []
        if os.path.lexists(self.paths.inbox):
            # A malformed filesystem object in the inbox is still a supplied
            # candidate.  Include it so activation records a deterministic
            # rejection instead of reporting that no rules were supplied.
            if not os.path.isfile(self.paths.inbox):
                candidates.append(("inbox", self.paths.inbox))
            else:
                checksum = sha256_file(self.paths.inbox)
                watcher = load_json_object(self.paths.watcher_state)
                if (
                    checksum == watcher.get("last_restart_checksum")
                    and checksum
                    != manifest.get("last_attempted_inbox_checksum")
                ):
                    candidates.append(("inbox", self.paths.inbox))
        recorded_platform = manifest.get("platform_identity")
        platform_changed = bool(
            recorded_platform and recorded_platform != self.platform_identity
        )
        if platform_changed:
            candidates.append(("packaged", self.paths.packaged))
            if not os.path.lexists(self.paths.active):
                candidates.append(("golden", self.paths.golden))
        elif os.path.lexists(self.paths.active):
            candidates.append(("active", self.paths.active))
        else:
            candidates.extend(
                (("packaged", self.paths.packaged), ("golden", self.paths.golden))
            )

        unique = []
        seen = set()
        for item in candidates:
            if item[1] not in seen:
                seen.add(item[1])
                unique.append(item)
        return unique

    def _record_attempt(
        self,
        manifest: Mapping[str, Any],
        source: str,
        checksum: str,
        validation: Optional[CandidateValidation] = None,
        failure_reason: str = "",
    ) -> Mapping[str, Any]:
        """Append one validation or pre-validation activation attempt."""

        if not isinstance(manifest, dict):
            return {}
        file_valid = validation.file_valid if validation else False
        usable_rule_count = validation.usable_rule_count if validation else 0
        broken_rule_count = len(validation.broken_rules) if validation else 0
        errors = list(validation.errors) if validation else [failure_reason]
        if validation is None:
            validation_result, activation_result = "FAILED", "FAILED"
        elif validation.activatable:
            validation_result = "DEGRADED" if validation.broken_rules else "PASSED"
            activation_result = "PENDING"
        else:
            validation_result, activation_result = "FAILED", "REJECTED"
            guard_reason = (
                "zero usable rules"
                if validation.file_valid and validation.usable_rule_count == 0
                else "file validation failed"
            )
            if guard_reason not in errors:
                errors.insert(0, guard_reason)
        attempt = {
            "source": source,
            "checksum": checksum,
            "at": floor_timestamp(self.clock()),
            "file_valid": file_valid,
            "usable_rule_count": usable_rule_count,
            "broken_rule_count": broken_rule_count,
            "validation_result": validation_result,
            "activation_result": activation_result,
            "reason": "; ".join(errors),
            "errors": errors,
        }
        self._append_attempt(manifest, attempt)
        if validation is not None and source == "inbox":
            manifest["last_attempted_inbox_checksum"] = checksum
        return attempt

    def _append_attempt(
        self, manifest: Mapping[str, Any], attempt: Mapping[str, Any]
    ) -> None:
        if not isinstance(manifest, dict):
            return
        history = manifest.get("activation_attempts", [])
        if not isinstance(history, list):
            history = []
        history = [dict(item) for item in history if isinstance(item, Mapping)]
        history.append(attempt)
        manifest["activation_attempts"] = history[-self.MAX_ACTIVATION_ATTEMPTS :]
        manifest["last_attempt"] = attempt

    def _promote(self, source_path: str, checksum: str) -> str:
        generation = "{}-{}".format(
            int(self.clock()), checksum.split(":", 1)[-1][:12]
        )
        versioned = os.path.join(
            self.paths.rules_dir, "dld_rules.{}.yaml".format(generation)
        )
        atomic_copy(source_path, versioned)
        atomic_copy(versioned, self.paths.active)
        return versioned

    def _archive_failed(self, source_path: str, checksum: str, source: str) -> None:
        checksum_id = checksum.split(":", 1)[-1][:12]
        suffix = "-{}-{}.yaml".format(source, checksum_id)
        if any(
            name.startswith("dld_rules.failed.") and name.endswith(suffix)
            for name in os.listdir(self.paths.rules_dir)
        ):
            return
        name = "dld_rules.failed.{}-{}-{}.yaml".format(
            int(self.clock()), source, checksum_id
        )
        destination = os.path.join(self.paths.rules_dir, name)
        atomic_copy(source_path, destination)

    def _prune_generations(self, manifest: Mapping[str, Any]) -> None:
        prefix = "dld_rules."
        suffix = ".yaml"
        paths = []
        for name in os.listdir(self.paths.rules_dir):
            if name.startswith(prefix) and name.endswith(suffix) and name != "dld_rules.active.yaml":
                path = os.path.join(self.paths.rules_dir, name)
                paths.append((os.path.getmtime(path), path))
        paths.sort(reverse=True)
        protected = {
            manifest.get("active_generation_path"),
        }
        protected.discard(None)
        kept = 0
        for _, path in paths:
            if path in protected or kept < self.retention:
                kept += 1
                continue
            try:
                os.unlink(path)
            except OSError as error:
                LOGGER.warning("unable to prune rules generation %s: %s", path, error)


class BrokenRuleStateStore:
    """Persist only broken rule diagnostics tied to a rules checksum."""

    STATE_SCHEMA = 1

    def __init__(self, path: str) -> None:
        self.path = path

    @staticmethod
    def _empty_state(recovery_error: Optional[str] = None) -> Mapping[str, Any]:
        """Build the canonical result for state that cannot be recovered."""

        state = {"broken_rules": [], "service_broken_count": 0}
        if recovery_error is not None:
            state["recovery_error"] = recovery_error
        return state

    def load(self, active_checksum: str, allow_crash_recovery: bool) -> Mapping[str, Any]:
        if not allow_crash_recovery:
            return self._empty_state()
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, ValueError) as error:
            LOGGER.warning("ignoring invalid DLDD state file %s: %s", self.path, error)
            return self._empty_state("invalid state file: {}".format(error))
        if not isinstance(state, Mapping):
            reason = "state file root must be an object"
            LOGGER.warning("ignoring invalid DLDD state file %s: %s", self.path, reason)
            return self._empty_state(reason)
        if state.get("clean_shutdown", False):
            return self._empty_state()
        if state.get("state_schema") != self.STATE_SCHEMA:
            return self._empty_state("state file schema is incompatible")
        if state.get("active_rules_checksum") != active_checksum:
            return self._empty_state()
        broken_rules = state.get("broken_rules")
        if not isinstance(broken_rules, list) or not all(
            isinstance(record, Mapping) for record in broken_rules
        ):
            reason = "state file broken_rules must be an array of objects"
            LOGGER.warning("ignoring invalid DLDD state file %s: %s", self.path, reason)
            return self._empty_state(reason)
        return state

    def save(
        self,
        active_checksum: str,
        broken_rules: Iterable[Mapping[str, Any]],
        clean_shutdown: bool = False,
    ) -> None:
        rules = [floor_timestamp_fields(rule) for rule in broken_rules]
        atomic_write_json(
            self.path,
            {
                "state_schema": self.STATE_SCHEMA,
                "active_rules_checksum": active_checksum,
                "broken_rules": rules,
                "service_broken_count": len(
                    set(rule.get("rule_id", rule.get("rule")) for rule in rules)
                ),
                "clean_shutdown": clean_shutdown,
                "updated_at": floor_timestamp(time.time()),
            },
        )

    def clear(self) -> None:
        unlink_if_exists(self.path)
