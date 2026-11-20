"""The build record contract, and what a record becomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from s2_connectors import build
from s2_connectors.contract import ContractError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "BLD-FC-2026.11.4.json"


def steps(record):
    return [(call.user, call.method, call.path) for call in build.plan(record)]


def test_the_worked_example_is_on_contract():
    record = build.load(EXAMPLE)
    assert record.build.buildId == "BLD-FC-2026.11.4"
    assert record.build.target == "FLIGHT"
    assert record.build.implements == ("SW-LOC-031",)
    assert len(record.testRuns) == 2


def test_the_build_id_reaches_every_run():
    """The record names the build once; each run still has to link to it."""
    record = build.load(EXAMPLE)
    assert [run.buildId for run in record.testRuns] == ["BLD-FC-2026.11.4"] * 2


def test_a_bench_kind_is_refused(build_document):
    """A HILS run does not come out of CI; it is reported on the bench contract."""
    build_document["testRuns"][0]["kind"] = "HILS"
    with pytest.raises(ContractError, match="kind"):
        build.parse(build_document)


def test_a_bench_field_is_refused(build_document):
    build_document["testRuns"][0]["bench"] = "HILS bench 2"
    with pytest.raises(ContractError):
        build.parse(build_document)


def test_a_malformed_digest_is_refused(build_document):
    """The digest is the one fact tying a record to a specific binary."""
    build_document["build"]["artifactDigest"] = "sha256:nothex"
    with pytest.raises(ContractError, match="artifactDigest"):
        build.parse(build_document)


def test_an_unknown_target_is_refused(build_document):
    build_document["build"]["target"] = "ORBITAL"
    with pytest.raises(ContractError):
        build.parse(build_document)


def test_another_contract_version_is_refused(build_document):
    build_document["contract"] = "s2.build-record/v2"
    with pytest.raises(ContractError):
        build.parse(build_document)


def test_a_record_with_no_runs_is_fine(build_document):
    """A build that has not been tested yet is still a build worth recording."""
    del build_document["testRuns"]
    record = build.parse(build_document)
    assert record.testRuns == ()
    assert [method for _, method, _ in steps(record)] == ["PUT", "PATCH", "POST"]


def test_the_sequence_the_worked_example_becomes():
    assert steps(build.load(EXAMPLE)) == [
        ("admin", "PUT", "/v1/objects/SoftwareBuild/BLD-FC-2026.11.4"),
        ("admin", "PATCH", "/v1/objects/SoftwareBuild/BLD-FC-2026.11.4"),
        ("admin", "POST", "/v1/objects/SoftwareBuild/BLD-FC-2026.11.4/links/implements"),
        ("admin", "PUT", "/v1/objects/TestRun/TR-UNIT-2026.11.4"),
        ("admin", "POST", "/v1/objects/TestRun/TR-UNIT-2026.11.4/links/testsBuild"),
        ("admin", "POST", "/v1/objects/TestRun/TR-UNIT-2026.11.4/links/verifies"),
        ("operator", "POST", "/v1/actions/recordTestVerdict/apply"),
        ("admin", "PUT", "/v1/objects/TestRun/TR-SITL-2026.11.4"),
        ("admin", "POST", "/v1/objects/TestRun/TR-SITL-2026.11.4/links/testsBuild"),
        ("admin", "POST", "/v1/objects/TestRun/TR-SITL-2026.11.4/links/ranOn"),
        ("admin", "POST", "/v1/objects/TestRun/TR-SITL-2026.11.4/links/verifies"),
        ("operator", "POST", "/v1/actions/recordTestVerdict/apply"),
        ("operator", "POST", "/v1/actions/advanceBuild/apply"),
    ]


def test_the_build_is_created_at_built_and_only_created():
    """A re-submitted record must not drag a build back from SIM_PASSED to BUILT."""
    create = build.plan(build.load(EXAMPLE))[0]
    assert create.headers == {"If-None-Match": "*"}
    assert create.tolerate_409 is True
    assert create.body["properties"]["status"] == "BUILT"


def test_the_refresh_leaves_status_alone():
    patch = build.plan(build.load(EXAMPLE))[1]
    assert patch.method == "PATCH"
    assert "status" not in patch.body["properties"]
    assert patch.body["properties"]["gitCommit"] == "c41e8a27d9b5"


def test_a_unit_run_is_not_linked_to_an_airframe():
    """Only a run bound to one drone gets ranOn; unit tests ran on nothing."""
    paths = [path for _, _, path in steps(build.load(EXAMPLE))]
    assert "/v1/objects/TestRun/TR-UNIT-2026.11.4/links/ranOn" not in paths
    assert "/v1/objects/TestRun/TR-SITL-2026.11.4/links/ranOn" in paths


def test_every_verdict_says_which_pipeline_run_it_came_from():
    verdicts = [c for c in build.plan(build.load(EXAMPLE)) if "recordTestVerdict" in c.path]
    assert len(verdicts) == 2
    for call in verdicts:
        assert "offline-ci/flight-software/4821" in call.body["rationale"]


def test_runs_are_written_without_their_verdicts():
    writes = [c for c in build.plan(build.load(EXAMPLE)) if c.path.startswith("/v1/objects/TestRun")]
    writes = [c for c in writes if c.method == "PUT"]
    assert len(writes) == 2
    for call in writes:
        assert call.body["properties"]["status"] == "RUNNING"
        assert "verdict" not in call.body["properties"]
