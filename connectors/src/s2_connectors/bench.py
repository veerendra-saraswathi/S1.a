"""The bench report contract: what a HILS or actuator bench emits when a run finishes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from s2_connectors.contract import check, read
from s2_connectors.model import (
    Anomaly,
    Call,
    Run,
    advance_build,
    attribute_anomaly,
    raise_anomaly,
    record_verdict,
    run_links,
    run_write,
)

CONTRACT = "s2.bench-report/v1"
SCHEMA = "bench-report-v1.schema.json"


@dataclass(frozen=True)
class BenchReport:
    run: Run
    anomalies: tuple[Anomaly, ...] = ()

    @property
    def source(self) -> str:
        """Named in the rationale of every action, so the audit log says where this came from."""
        where = f" from {self.run.bench}" if self.run.bench else ""
        return f"bench report {self.run.testRunId}{where}"


def parse(document: Any) -> BenchReport:
    check(document, SCHEMA)
    # additionalProperties is false throughout the schema, so every key here is a field.
    run = dict(document["run"])
    run["verifies"] = tuple(run.get("verifies", ()))
    return BenchReport(
        run=Run(**run),
        anomalies=tuple(Anomaly(**anomaly) for anomaly in document.get("anomalies", [])),
    )


def load(path: str | Path) -> BenchReport:
    return parse(read(path))


def plan(report: BenchReport) -> list[Call]:
    """Every call this report becomes, in the order it is sent.

    Anomalies are raised before the verdict is recorded, so the verdict can refer to
    them and a closed run is never left with its anomalies still missing. A HILS run
    that passed then asks for the build's bench step, last, so the gate is only ever
    asked about a run whose verdict is already on record.
    """
    run, source = report.run, report.source
    calls = [run_write(run), *run_links(run)]
    for anomaly in report.anomalies:
        calls.append(raise_anomaly(run, anomaly, source))
        if anomaly.attributed:
            calls.append(attribute_anomaly(anomaly, source))
    calls.append(record_verdict(run, source))
    step = advance_build(run, source)
    if step:
        calls.append(step)
    return calls
