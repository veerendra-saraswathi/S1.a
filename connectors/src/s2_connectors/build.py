"""The build record contract: what a CI pipeline emits when it finishes a build.

This is the left-hand end of the traceability chain. Until something writes builds,
a `SoftwareBuild` in S2 exists only because a person put it there, and "which build
satisfies this requirement, and how was it tested" has no answer the pipeline stands
behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from s2_connectors.contract import check, read
from s2_connectors.model import ADMIN, Call, Run, record_verdict, run_links, run_write

CONTRACT = "s2.build-record/v1"
SCHEMA = "build-record-v1.schema.json"


@dataclass(frozen=True)
class Build:
    buildId: str
    name: str
    target: str
    gitCommit: str | None = None
    artifactDigest: str | None = None
    builtAt: str | None = None
    pipelineRun: str | None = None
    implements: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildRecord:
    build: Build
    testRuns: tuple[Run, ...] = ()

    @property
    def source(self) -> str:
        """Named in the rationale of every action, so the audit log says where this came from."""
        run = f", pipeline run {self.build.pipelineRun}" if self.build.pipelineRun else ""
        return f"build record for {self.build.buildId}{run}"


def parse(document: Any) -> BuildRecord:
    check(document, SCHEMA)
    build = dict(document["build"])
    build["implements"] = tuple(build.get("implements", ()))
    build_id = build["buildId"]

    runs = []
    for entry in document.get("testRuns", []):
        run = dict(entry)
        run["verifies"] = tuple(run.get("verifies", ()))
        # The record names the build once; every run in it tested that build.
        run["buildId"] = build_id
        runs.append(Run(**run))

    return BuildRecord(build=Build(**build), testRuns=tuple(runs))


def load(path: str | Path) -> BuildRecord:
    return parse(read(path))


def _build_properties(build: Build) -> dict[str, Any]:
    properties: dict[str, Any] = {"name": build.name, "target": build.target}
    for key in ("gitCommit", "artifactDigest", "builtAt"):
        value = getattr(build, key)
        if value is not None:
            properties[key] = value
    return properties


def plan(record: BuildRecord) -> list[Call]:
    """Every call this record becomes, in the order it is sent."""
    build, source = record.build, record.source
    path = f"/v1/objects/SoftwareBuild/{build.buildId}"
    properties = _build_properties(build)

    calls = [
        # Create only, guarded by If-None-Match. A build that has since advanced to
        # SIM_PASSED or further must not be dragged back to BUILT by a re-submitted
        # record, and a PUT replaces the whole object. S2 answers 409 when it already
        # exists, which here means "already recorded", not a failure.
        Call(
            ADMIN,
            "PUT",
            path,
            {"properties": {**properties, "status": "BUILT"}},
            headers={"If-None-Match": "*"},
            tolerate_409=True,
        ),
        # Then refresh the metadata without touching status. A PATCH merges, and S2
        # does not audit a write that changes nothing, so this is free on a re-run.
        Call(ADMIN, "PATCH", path, {"properties": properties}),
    ]
    calls += [
        Call(ADMIN, "POST", f"{path}/links/implements", {"target_primary_key": key})
        for key in build.implements
    ]

    for run in record.testRuns:
        calls.append(run_write(run))
        calls += run_links(run)
        calls.append(record_verdict(run, source))

    return calls
