"""Command-line entry point for DLDD and offline validation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess

from .config import DLDDConfig
from .dse import DSERegistry
from .hooks import VendorHookError
from .lifecycle import RulePaths
from .platform import PlatformIdentity, detect_identity, load_extensions
from .preflight import preflight_activation
from .qualification import qualify_e2e
from .reset import clear_runtime_state
from .service import run_service
from .telemetry import SonicStateDB
from .validation import ValidationContext, load_rules, source_line_for_path


def _issue_payload(issue):
    return {
        "scope": issue.scope,
        "code": issue.code,
        "message": issue.message,
        "path": issue.path,
        "rule_name": issue.rule_name,
        "rule_id": issue.rule_id,
        "line": issue.line,
    }


def _issue_location(issue):
    if issue.get("line") is None:
        return issue["path"]
    return "{} (line {})".format(issue["path"], issue["line"])


def validate_rules(args) -> int:
    static_only = args.mode == "static-schema"
    identity = (
        PlatformIdentity("static-schema", None, None)
        if static_only
        else detect_identity()
    )
    platform_dir = args.platform_dir or "/usr/share/sonic/device/{}".format(
        identity.platform
    )
    dse_path = args.dse or os.path.join(platform_dir, "dld_dse.yaml")
    extensions = None if static_only else load_extensions(identity, dse_path)
    compatibility_required = args.mode not in ("static-schema", "dse-resolve")
    context = ValidationContext(
        product_id=identity.product_id if compatibility_required else None,
        software_version=identity.software_version if compatibility_required else None,
        require_compatibility_identity=compatibility_required,
        dse_registry=(
            extensions.dse_registry
            if extensions
            else DSERegistry()
        ),
        compatibility_matcher=(
            extensions.compatibility_matcher
            if extensions
            else ValidationContext().compatibility_matcher
        ),
    )
    result = load_rules(args.file, context, materialize=not static_only)
    valid_rule_count = (
        len(result.ruleset.signatures)
        if static_only and result.ruleset is not None
        else len(result.materialized_rules)
    )
    payload = {
        "schema_version": result.schema_version,
        "rules_parsed_successfully": valid_rule_count,
        "rules_failed_validation": len(result.broken_rules),
        "file_level_result": "PASSED" if result.file_valid else "FAILED",
        "rule_level_result": (
            "PASSED"
            if not result.broken_rules
            else "DEGRADED"
            if valid_rule_count
            else "FAILED"
        ),
        "file_errors": [_issue_payload(issue) for issue in result.file_errors],
        "broken_rules": [
            {
                "rule": item.rule_name,
                "rule_id": item.rule_id,
                "issues": [_issue_payload(issue) for issue in item.issues],
            }
            for item in result.broken_rules
        ],
    }
    probe_failed = False
    if args.mode in (
        "activation-dry-run",
        "hardware-probe",
        "e2e-execute",
    ) and result.activation_valid:
        preflight = preflight_activation(
            result,
            extensions,
            DLDDConfig().polling_intervals,
        )
        adapters = preflight.adapters
        bundle = preflight.plan
        probe_results = []
        invalid_rule_ids = set(preflight.invalid_rule_ids)
        invalid_reasons = {
            failure.rule_id: (failure.code, failure.message)
            for failure in preflight.failures
        }
        for failure in preflight.failures:
            failure_result = {
                "correlation_key": failure.correlation_key,
                "state": "FAILED",
                "error": failure.message,
            }
            if args.mode == "e2e-execute":
                failure_result.update(
                    rule=failure.rule_name,
                    rule_id=failure.rule_id,
                    event_id=None,
                    component=None,
                    stage="preflight",
                )
            probe_results.append(failure_result)
        if args.mode in ("hardware-probe", "e2e-execute"):
            probe_failed = bool(preflight.failures)
        if args.mode == "e2e-execute":
            qualification = qualify_e2e(
                bundle, adapters, invalid_rule_ids
            )
            probe_results.extend(qualification.event_results)
            payload["rule_results"] = list(qualification.rule_results)
            probe_failed = probe_failed or qualification.failed
        if args.mode == "activation-dry-run":
            for item in bundle.work_items.values():
                if item.rule_id not in invalid_rule_ids:
                    probe_results.append(
                        {
                            "correlation_key": item.correlation_key,
                            "state": "VALID",
                        }
                    )
        elif args.mode == "hardware-probe":
            for item in bundle.work_items.values():
                if item.rule_id in invalid_rule_ids:
                    continue
                try:
                    adapter = adapters[item.source_type]
                    adapter.get_value(item)
                    state = "AVAILABLE"
                    probe_results.append(
                        {
                            "correlation_key": item.correlation_key,
                            "state": state,
                        }
                    )
                except (ValueError, VendorHookError) as error:
                    probe_failed = True
                    invalid_rule_ids.add(item.rule_id)
                    invalid_reasons.setdefault(
                        item.rule_id,
                        ("adapter_validation_failed", str(error)),
                    )
                    probe_results.append(
                        {
                            "correlation_key": item.correlation_key,
                            "state": "FAILED",
                            "error": str(error),
                        }
                    )
                except Exception as error:
                    probe_failed = True
                    invalid_rule_ids.add(item.rule_id)
                    invalid_reasons.setdefault(
                        item.rule_id,
                        ("adapter_probe_failed", str(error)),
                    )
                    probe_results.append(
                        {
                            "correlation_key": item.correlation_key,
                            "state": "FAILED",
                            "error": str(error),
                        }
                    )
        payload["probe_results"] = probe_results
        if invalid_rule_ids:
            invalid_rules = {
                rule.signature.metadata.id: rule.signature.metadata.name
                for rule in result.materialized_rules
                if rule.signature.metadata.id in invalid_rule_ids
            }
            for rule_id, rule_name in sorted(invalid_rules.items()):
                code, message = invalid_reasons[rule_id]
                payload["broken_rules"].append(
                    {
                        "rule": rule_name,
                        "rule_id": rule_id,
                        "issues": [
                            {
                                "scope": "rule",
                                "code": code,
                                "message": message,
                                "path": "$.signatures",
                                "rule_name": rule_name,
                                "rule_id": rule_id,
                                "line": source_line_for_path(
                                    result.source_lines, "$.signatures"
                                ),
                            }
                        ],
                    }
                )
            valid_rule_count -= len(invalid_rules)
            payload["rules_parsed_successfully"] = valid_rule_count
            payload["rules_failed_validation"] = len(payload["broken_rules"])
            payload["rule_level_result"] = (
                "DEGRADED" if valid_rule_count else "FAILED"
            )
    if args.mode == "e2e-execute":
        payload["qualification_result"] = (
            "PASSED"
            if result.activation_valid
            and valid_rule_count > 0
            and not probe_failed
            else "FAILED"
        )
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print("Schema version: {}".format(result.schema_version or "unknown"))
        print("Rules parsed successfully: {}".format(valid_rule_count))
        print("Rules failed validation: {}".format(len(payload["broken_rules"])))
        for item in payload["broken_rules"]:
            for issue in item["issues"]:
                print(
                    "  - {}: {}: {} ({})".format(
                        item["rule"],
                        _issue_location(issue),
                        issue["message"],
                        issue["code"],
                    )
                )
        for issue in payload["file_errors"]:
            print(
                "  - file: {}: {} ({})".format(
                    _issue_location(issue), issue["message"], issue["code"]
                )
            )
        print("File-level result: {}".format(payload["file_level_result"]))
        print("Rule-level result: {}".format(payload["rule_level_result"]))
        if args.mode == "e2e-execute":
            print(
                "Qualification result: {}".format(
                    payload["qualification_result"]
                )
            )
            for probe in payload.get("probe_results", ()):
                detail = probe.get("error") or probe.get("reason")
                print(
                    "  event {}:{}:{} [{}]: {}{}".format(
                        probe.get("rule", "unknown"),
                        probe.get("event_id", "-"),
                        probe.get("component") or "unresolved",
                        probe.get("stage", "execution"),
                        probe["state"],
                        " ({})".format(detail) if detail else "",
                    )
                )
            for rule in payload.get("rule_results", ()):
                detail = rule.get("reason")
                print(
                    "  rule {}:{} [{}]: {}{}".format(
                        rule["rule"],
                        rule.get("component") or "unresolved",
                        rule["stage"],
                        rule["state"],
                        " ({})".format(detail) if detail else "",
                    )
                )
        elif args.verbose:
            for rule in result.materialized_rules:
                print(
                    "  materialized {} with {} event(s)".format(
                        rule.signature.metadata.name, len(rule.events)
                    )
                )
            for probe in payload.get("probe_results", ()):
                print(
                    "  probe {}: {}".format(
                        probe["correlation_key"], probe["state"]
                    )
                )
    valid = result.file_valid and valid_rule_count > 0 and not probe_failed
    return 0 if valid else 1


def clear_state(args) -> int:
    """Stop DLDD if needed and clear daemon-owned runtime state."""

    active = subprocess.run(
        ["/bin/systemctl", "is-active", "--quiet", "dldd.service"],
        shell=False,
        check=False,
    ).returncode == 0
    if active:
        try:
            subprocess.run(
                ["/bin/systemctl", "stop", "dldd.service"],
                shell=False,
                check=True,
            )
        except Exception as error:
            logging.getLogger(__name__).error(
                "unable to stop DLDD for state cleanup: %s", error
            )
            return 1
    try:
        paths = RulePaths(platform_dir="")
        result = clear_runtime_state(
            SonicStateDB(),
            paths.state_file,
            include_faults=args.all,
            include_artifacts=args.all,
        )
    except Exception as error:
        logging.getLogger(__name__).error("unable to clear DLDD state: %s", error)
        return_code = 1
    else:
        print(
            "Cleared DLDD runtime state: {} Redis key(s), {} fault(s), "
            "{} artifact file(s).".format(
                result.redis_keys, result.faults, result.artifacts
            )
        )
        return_code = 0
    finally:
        if active:
            try:
                subprocess.run(
                    ["/bin/systemctl", "start", "dldd.service"],
                    shell=False,
                    check=True,
                )
            except Exception as error:
                logging.getLogger(__name__).error(
                    "unable to restart DLDD after state cleanup: %s", error
                )
                return_code = 1
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dldd")
    parser.add_argument("--log-level", default="INFO")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("run", help="run the Device Local Diagnosis service")
    reset = subcommands.add_parser(
        "clear-state", help="clear DLDD-owned runtime state"
    )
    reset.add_argument(
        "--all",
        action="store_true",
        help=(
            "also remove DLDD-owned FAULT_INFO rows and diagnostic artifacts; "
            "rules and configuration are preserved"
        ),
    )
    validation = subcommands.add_parser("validate-rules", help="validate a rules file")
    validation.add_argument("--file", required=True)
    validation.add_argument("--dse")
    validation.add_argument("--platform-dir")
    validation.add_argument("--verbose", action="store_true")
    validation.add_argument("--json", action="store_true")
    validation.add_argument(
        "--mode",
        choices=(
            "static-schema",
            "dse-resolve",
            "activation-dry-run",
            "hardware-probe",
            "e2e-execute",
        ),
        default="activation-dry-run",
        help=(
            "validation strength; hardware-probe reads live sources and "
            "e2e-execute expands DSE instances and performs one live, "
            "non-remediating collection/comparison pass"
        ),
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if args.command == "validate-rules":
        return validate_rules(args)
    if args.command == "clear-state":
        return clear_state(args)
    # argparse rejects unknown subcommands, so the only remaining choices are
    # the explicit ``run`` command and the backward-compatible empty command.
    run_service()
    return 0
