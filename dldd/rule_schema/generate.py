"""Generate the derivative JSON Schema published for DLDD rule authors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .registry import DEFAULT_CONTRACT_REGISTRY


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
GENERATED_WARNING = (
    "Generated from the installed DLDD Pydantic contract; do not edit by hand. "
    "The daemon does not load this file at runtime."
)


def default_output_path(version: str) -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / (
        "dld-rules-{}.json".format(version)
    )


def schema_id(version: str) -> str:
    return (
        "https://sonic-net.github.io/dldd/schemas/"
        "dld-rules-{}.json".format(version)
    )


def generate_schema(version: str) -> Mapping:
    contract = DEFAULT_CONTRACT_REGISTRY.require_exact(version)
    generated = contract.document_model.model_json_schema(
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    generated["$schema"] = JSON_SCHEMA_DIALECT
    generated["$id"] = schema_id(version)
    generated["title"] = "SONiC Device Local Diagnosis rules {}".format(
        version
    )
    generated["x-dldd-schema-version"] = version
    generated["x-generated-warning"] = GENERATED_WARNING
    return generated


def render_schema(version: str) -> str:
    return json.dumps(
        generate_schema(version),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify a derivative DLDD JSON Schema"
    )
    parser.add_argument(
        "--version",
        required=True,
        choices=DEFAULT_CONTRACT_REGISTRY.versions,
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output path (defaults to the packaged schemas directory)",
    )
    parser.add_argument(
        "--check",
        nargs="?",
        const="",
        metavar="PATH",
        help=(
            "compare generated output with PATH; when PATH is omitted, use "
            "--output or the packaged schema path"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or default_output_path(args.version)
    rendered = render_schema(args.version)

    if args.check is not None:
        target = Path(args.check) if args.check else output
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit(
                "unable to read generated schema {}: {}".format(target, error)
            )
        if current != rendered:
            raise SystemExit(
                "generated DLDD schema is out of date: {}".format(target)
            )
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module
    raise SystemExit(main())


__all__ = ("generate_schema", "render_schema")
