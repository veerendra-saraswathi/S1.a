"""Mapping a bench report onto S2 objects, links and actions.

`plan` is pure: it turns a report into the exact sequence of calls that would be sent,
which is what the tests assert against and what `--dry-run` prints. `submit` is the only
part that talks to an engine.

Who does what follows the ontology. The run and its links are ordinary writes and need
`ontology-admin`, which the `admin` user holds. The anomalies and the verdict are
governed actions and are requested by `operator`, because a bench verdict is an event
that must not be lost and therefore goes through the API as an action rather than over
the telemetry path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from s2_hils.client import S2Client, S2Error
from s2_hils.report import Anomaly, Report, Run

ADMIN = "admin"
OPERATOR = "operator"


@dataclass(frozen=True)
class Call:
    user: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    idempotency_key: str | None = None
    tolerate_409: bool = False


def _run_properties(run: Run) -> dict[str, Any]:
    """The TestRun's state, minus its verdict.

    The run is written as RUNNING even when the bench reports it finished: `status` and
    `verdict` are the `recordTestVerdict` action's to set, and writing them here would
    put a governed state in an ordinary PUT. A PUT replaces the whole object, so only
    the properties the bench supplied are sent; the rest stay unset.
    """
    properties: dict[str, Any] = {"name": run.name, "kind": run.kind, "status": "RUNNING"}
    for key in ("bench", "startedAt", "endedAt", "logUri"):
        value = getattr(run, key)
        if value is not None:
            properties[key] = value
    return properties


def _raise_anomaly(run: Run, anomaly: Anomaly) -> Call:
    parameters: dict[str, Any] = {
        "anomalyId": anomaly.anomalyId,
        "testRunId": run.testRunId,
        "title": anomaly.title,
        "severity": anomaly.severity,
    }
    for key in ("detector", "faultClass", "detectedAt", "durationS"):
        value = getattr(anomaly, key)
        if value is not None:
            parameters[key] = value
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/raiseAnomaly/apply",
        {"parameters": parameters},
        idempotency_key=f"hils:{run.testRunId}:raiseAnomaly:{anomaly.anomalyId}",
        # Re-submitting a report finds the anomaly already there. S2 answers 409 and
        # changes nothing, which is the right outcome, not a failure.
        tolerate_409=True,
    )


def _attribute_anomaly(anomaly: Anomaly) -> Call:
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/attributeAnomaly/apply",
        {
            "parameters": {
                "anomalyId": anomaly.anomalyId,
                "subsystemId": anomaly.subsystemId,
                "rootCause": anomaly.rootCause,
            }
        },
        idempotency_key=f"hils:{anomaly.anomalyId}:attributeAnomaly:{anomaly.subsystemId}",
    )


def _record_verdict(run: Run) -> Call:
    parameters: dict[str, Any] = {"testRunId": run.testRunId, "verdict": run.verdict}
    if run.verdictNote is not None:
        parameters["note"] = run.verdictNote
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/recordTestVerdict/apply",
        {"parameters": parameters},
        # The verdict is part of the key, so a bench that corrects a verdict sends a new
        # request rather than replaying the old one.
        idempotency_key=f"hils:{run.testRunId}:recordTestVerdict:{run.verdict}",
    )


def plan(report: Report) -> list[Call]:
    """Every call this report becomes, in the order it is sent.

    Anomalies are raised before the verdict is recorded, so the verdict can refer to
    them and so a run never sits closed with its anomalies still missing.
    """
    run = report.run
    calls = [Call(ADMIN, "PUT", f"/v1/objects/TestRun/{run.testRunId}", {"properties": _run_properties(run)})]

    links = f"/v1/objects/TestRun/{run.testRunId}/links"
    if run.buildId:
        calls.append(Call(ADMIN, "POST", f"{links}/testsBuild", {"target_primary_key": run.buildId}))
    if run.droneKey:
        calls.append(Call(ADMIN, "POST", f"{links}/ranOn", {"target_primary_key": run.droneKey}))
    calls += [Call(ADMIN, "POST", f"{links}/verifies", {"target_primary_key": key}) for key in run.verifies]

    for anomaly in report.anomalies:
        calls.append(_raise_anomaly(run, anomaly))
        if anomaly.attributed:
            calls.append(_attribute_anomaly(anomaly))

    calls.append(_record_verdict(run))
    return calls


def submit(client: S2Client, report: Report, log: Callable[[str], None] = print) -> None:
    """Send the plan, stopping at the first refusal S2 did not expect."""
    for call in plan(report):
        response = client.request(call.user, call.method, call.path, call.body, call.idempotency_key)
        if call.tolerate_409 and response.status_code == 409:
            log(f"409 {call.method} {call.path}  unchanged: {response.text[:160]}")
            continue
        if response.status_code >= 400:
            hint = ""
            if response.status_code == 404:
                hint = "\nthe build, drone or requirement this run refers to is not in S2 yet."
            raise S2Error(f"{call.method} {call.path} -> {response.status_code}\n{response.text}{hint}")
        note = "  awaiting approval" if response.status_code == 202 else ""
        if response.headers.get("Idempotent-Replay") == "true":
            note += "  (replay)"
        log(f"{response.status_code} {call.method} {call.path}{note}")
