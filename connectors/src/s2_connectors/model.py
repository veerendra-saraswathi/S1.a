"""One call, and the mappings every connector shares.

Both connectors describe test runs, link them to what they exercised, and close them
with a verdict. Those mappings live here so the two cannot drift: a run reported by a
bench and a run reported by a build pipeline become the same thing in S2.

Who does what follows the ontology. Objects and links are ordinary writes and need
`ontology-admin`, which `admin` holds. Verdicts and anomalies are governed actions,
requested by `operator`, and each carries a rationale naming the document it came from:
S2 shows that to an approver and keeps it in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ADMIN = "admin"
OPERATOR = "operator"

# Which step of the build gate a passed run of each kind earns. A unit run earns none:
# passing unit tests is what makes a build worth simulating, not evidence it flies. An
# actuator bench exercises a part, not the build, and a flight comes after clearance.
GATE_STAGE = {"SITL": "SIM_PASSED", "SIMULATION": "SIM_PASSED", "HILS": "BENCH_PASSED"}


@dataclass(frozen=True)
class Call:
    """One request a document becomes."""

    user: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    idempotency_key: str | None = None
    headers: dict[str, str] | None = None
    tolerate_409: bool = False
    # A 403 here is the build gate holding, which is an answer, not a failure.
    tolerate_denied: bool = False


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
        """Whether the reporter named both a subsystem and a cause.

        The contracts require the two together, so one without the other cannot occur.
        Without them the anomaly is left OPEN for a person to attribute.
        """
        return bool(self.subsystemId and self.rootCause)


@dataclass(frozen=True)
class Run:
    """One test execution: what was exercised, against what, and how it came out."""

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


def _present(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: getattr(source, key) for key in keys if getattr(source, key) is not None}


def run_write(run: Run) -> Call:
    """Write the run, minus its verdict.

    The run is written as RUNNING even when the reporter says it finished: `status` and
    `verdict` are `recordTestVerdict`'s to set, and writing them here would put a
    governed state into an ordinary write. A PUT replaces the whole object, so only the
    properties the reporter supplied are sent and the rest stay unset.
    """
    properties = {"name": run.name, "kind": run.kind, "status": "RUNNING"}
    properties.update(_present(run, ("bench", "startedAt", "endedAt", "logUri")))
    return Call(ADMIN, "PUT", f"/v1/objects/TestRun/{run.testRunId}", {"properties": properties})


def run_links(run: Run) -> list[Call]:
    """Link the run to the build it tested, the drone it ran on and what it verifies."""
    base = f"/v1/objects/TestRun/{run.testRunId}/links"
    calls = []
    if run.buildId:
        calls.append(Call(ADMIN, "POST", f"{base}/testsBuild", {"target_primary_key": run.buildId}))
    if run.droneKey:
        calls.append(Call(ADMIN, "POST", f"{base}/ranOn", {"target_primary_key": run.droneKey}))
    calls += [Call(ADMIN, "POST", f"{base}/verifies", {"target_primary_key": key}) for key in run.verifies]
    return calls


def raise_anomaly(run: Run, anomaly: Anomaly, source: str) -> Call:
    parameters = {
        "anomalyId": anomaly.anomalyId,
        "testRunId": run.testRunId,
        "title": anomaly.title,
        "severity": anomaly.severity,
    }
    parameters.update(_present(anomaly, ("detector", "faultClass", "detectedAt", "durationS")))
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/raiseAnomaly/apply",
        {"parameters": parameters, "rationale": f"Raised from {source}."},
        idempotency_key=f"{run.testRunId}:raiseAnomaly:{anomaly.anomalyId}",
        # Re-submitting a report finds the anomaly already there. S2 answers 409 and
        # changes nothing, which is the right outcome, not a failure.
        tolerate_409=True,
    )


def attribute_anomaly(anomaly: Anomaly, source: str) -> Call:
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/attributeAnomaly/apply",
        {
            "parameters": {
                "anomalyId": anomaly.anomalyId,
                "subsystemId": anomaly.subsystemId,
                "rootCause": anomaly.rootCause,
            },
            "rationale": f"Attributed from {source}.",
        },
        idempotency_key=f"{anomaly.anomalyId}:attributeAnomaly:{anomaly.subsystemId}",
    )


def record_verdict(run: Run, source: str) -> Call:
    parameters: dict[str, Any] = {"testRunId": run.testRunId, "verdict": run.verdict}
    if run.verdictNote is not None:
        parameters["note"] = run.verdictNote
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/recordTestVerdict/apply",
        {"parameters": parameters, "rationale": f"Recorded from {source}."},
        # The verdict is part of the key, so a reporter that corrects a verdict sends a
        # new request rather than replaying the old one.
        idempotency_key=f"{run.testRunId}:recordTestVerdict:{run.verdict}",
    )


def advance_build(run: Run, source: str) -> Call | None:
    """Ask for the gate step a passed run earns, or nothing when it earns none.

    Whether the step is taken is the policy's call. A HILS pass on a build that has not
    passed simulation is refused, and so is a second run asking for a step the build
    already holds; S2 answers 403 with the reason, and the submission reports it and
    carries on. The run's verdict has been recorded either way.
    """
    stage = GATE_STAGE.get(run.kind)
    if stage is None or run.verdict != "PASS" or not run.buildId:
        return None
    return Call(
        OPERATOR,
        "POST",
        "/v1/actions/advanceBuild/apply",
        {
            "parameters": {
                "buildId": run.buildId,
                "stage": stage,
                "testRunId": run.testRunId,
                "note": f"{run.testRunId} passed.",
            },
            "rationale": f"Advanced from {source}.",
        },
        # One request per build, stage and run: a re-submitted document replays, and a
        # different run asking for the same step is a new request the policy answers.
        idempotency_key=f"{run.buildId}:advanceBuild:{stage}:{run.testRunId}",
        tolerate_denied=True,
    )
