"""Loading and validation of bench reports.

The JSON Schema beside this package is the contract a bench writes against, so
validation happens here and nowhere else. By the time a `Report` exists every
enumeration and required field has been checked, and the submission code can read it
without defending itself against malformed input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

CONTRACT = "s2.bench-report/v1"

# The schema is read from the source tree rather than from package data: it is a
# published contract that other teams read directly, so it lives where they can find it.
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "bench-report-v1.schema.json"


class ReportError(ValueError):
    """A report that cannot be trusted: unreadable, not JSON, or off-contract."""


@dataclass(frozen=True)
class Anomaly:
    """One flagged window in a run."""

    anomalyId: str
    title: str
    severity: str
    detector: str | None = None
    faultClass: str | None = None
    detectedAt: str | None = None
    durationS: float | None = None
    subsystemId: str | None = None
    rootCause: str | None = None

    @property
    def attributed(self) -> bool:
        """Whether the bench named both a subsystem and a cause.

        The schema requires the two together, so one without the other cannot occur.
        Without them the anomaly is left OPEN for a person to attribute.
        """
        return bool(self.subsystemId and self.rootCause)


@dataclass(frozen=True)
class Run:
    """The run itself: what was exercised, against what, and how it came out."""

    testRunId: str
    name: str
    kind: str
    verdict: str
    startedAt: str
    endedAt: str | None = None
    bench: str | None = None
    verdictNote: str | None = None
    logUri: str | None = None
    buildId: str | None = None
    droneKey: str | None = None
    verifies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    run: Run
    anomalies: tuple[Anomaly, ...] = ()


def _schema() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_PATH.read_text())
    except OSError as exc:
        raise ReportError(f"the bench report schema is not readable at {SCHEMA_PATH}: {exc}") from exc


def parse(document: dict[str, Any]) -> Report:
    """Validate a decoded report against the contract and return it as objects."""
    try:
        jsonschema.validate(document, _schema(), format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        where = "/".join(str(part) for part in exc.absolute_path) or "(root)"
        raise ReportError(f"{where}: {exc.message}") from exc

    # additionalProperties is false throughout the schema, so every key here is a field.
    run = dict(document["run"])
    run["verifies"] = tuple(run.get("verifies", ()))
    return Report(
        run=Run(**run),
        anomalies=tuple(Anomaly(**anomaly) for anomaly in document.get("anomalies", [])),
    )


def load(path: str | Path) -> Report:
    """Read a report from disk and validate it."""
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ReportError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ReportError(f"{path}: a report is a JSON object, not {type(document).__name__}")
    return parse(document)
