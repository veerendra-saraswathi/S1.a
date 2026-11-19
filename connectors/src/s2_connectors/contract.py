"""The published contracts, and checking a document against one.

Each contract is a JSON Schema in `connectors/schema`, read from the source tree rather
than from package data: these are specifications other teams write against, so they live
where people can find them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"


class ContractError(ValueError):
    """A document that cannot be trusted: unreadable, not JSON, or off-contract."""


def _schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise ContractError(f"the schema is not readable at {path}: {exc}") from exc


def check(document: Any, schema_name: str) -> dict[str, Any]:
    """Validate a decoded document, returning it, or raise naming the offending field."""
    if not isinstance(document, dict):
        raise ContractError(f"a report is a JSON object, not {type(document).__name__}")
    try:
        jsonschema.validate(document, _schema(schema_name), format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        where = "/".join(str(part) for part in exc.absolute_path) or "(root)"
        raise ContractError(f"{where}: {exc.message}") from exc
    return document


def read(path: str | Path) -> Any:
    """Decode a document from disk. Validation is the caller's next step."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path} is not valid JSON: {exc}") from exc
