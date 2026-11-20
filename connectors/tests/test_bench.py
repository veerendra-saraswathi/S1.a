"""The bench report contract, and what a report becomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from s2_connectors import bench
from s2_connectors.contract import ContractError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "TR-HILS-0058.json"


def steps(report):
    return [(call.user, call.method, call.path) for call in bench.plan(report)]


def test_the_worked_example_is_on_contract():
    report = bench.load(EXAMPLE)
    assert report.run.testRunId == "TR-HILS-0058"
    assert report.run.verdict == "FAIL"
    assert report.run.verifies == ("SYS-NAV-012",)
    assert len(report.anomalies) == 2


def test_the_window_keeps_its_type():
    """durationS must stay a number: S2 holds it as a double, not a string."""
    anomaly = bench.load(EXAMPLE).anomalies[0]
    assert anomaly.durationS == pytest.approx(6.6)
    assert isinstance(anomaly.durationS, float)


def test_an_anomaly_is_attributed_only_when_both_halves_are_present():
    report = bench.load(EXAMPLE)
    assert report.anomalies[0].attributed is True
    assert report.anomalies[1].attributed is False
    assert report.anomalies[1].subsystemId is None


def test_another_contract_version_is_refused(bench_document):
    bench_document["contract"] = "s2.bench-report/v2"
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_an_unknown_fault_class_is_refused(bench_document):
    bench_document["anomalies"][0]["faultClass"] = "GREMLINS"
    with pytest.raises(ContractError, match="faultClass"):
        bench.parse(bench_document)


def test_an_unknown_verdict_is_refused(bench_document):
    bench_document["run"]["verdict"] = "PROBABLY_FINE"
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_a_subsystem_without_a_cause_is_refused(bench_document):
    """Attributing a fault to a subsystem without saying why is not an attribution."""
    del bench_document["anomalies"][0]["rootCause"]
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_a_cause_without_a_subsystem_is_refused(bench_document):
    del bench_document["anomalies"][0]["subsystemId"]
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_a_negative_window_is_refused(bench_document):
    bench_document["anomalies"][0]["durationS"] = -1
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_an_unknown_field_is_refused(bench_document):
    """A bench that invents a field has diverged from the contract and should be told."""
    bench_document["run"]["temperatureC"] = 41
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_a_missing_run_is_refused(bench_document):
    del bench_document["run"]
    with pytest.raises(ContractError):
        bench.parse(bench_document)


def test_a_report_that_is_not_json_says_so(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json")
    with pytest.raises(ContractError, match="not valid JSON"):
        bench.load(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(ContractError, match="cannot read"):
        bench.load(tmp_path / "absent.json")


def test_the_sequence_the_worked_example_becomes():
    assert steps(bench.load(EXAMPLE)) == [
        ("admin", "PUT", "/v1/objects/TestRun/TR-HILS-0058"),
        ("admin", "PATCH", "/v1/objects/TestRun/TR-HILS-0058"),
        ("admin", "POST", "/v1/objects/TestRun/TR-HILS-0058/links/testsBuild"),
        ("admin", "POST", "/v1/objects/TestRun/TR-HILS-0058/links/ranOn"),
        ("admin", "POST", "/v1/objects/TestRun/TR-HILS-0058/links/verifies"),
        ("operator", "POST", "/v1/actions/raiseAnomaly/apply"),
        ("operator", "POST", "/v1/actions/attributeAnomaly/apply"),
        ("operator", "POST", "/v1/actions/raiseAnomaly/apply"),
        ("operator", "POST", "/v1/actions/recordTestVerdict/apply"),
    ]


def test_the_run_is_written_without_its_verdict():
    """status and verdict belong to recordTestVerdict, not to an ordinary PUT."""
    properties = bench.plan(bench.load(EXAMPLE))[0].body["properties"]
    assert properties["status"] == "RUNNING"
    assert "verdict" not in properties
    assert properties["logUri"] == "s3://s2-bench-logs/TR-HILS-0058/"


def test_the_run_is_only_created_and_its_refresh_leaves_the_verdict_alone():
    """A re-sent report must not put a closed run back to RUNNING and wipe its verdict.

    The verdict request only replays on a re-send, so it would not set the verdict again.
    """
    create, refresh = bench.plan(bench.load(EXAMPLE))[:2]
    assert create.headers == {"If-None-Match": "*"}
    assert create.tolerate_409 is True
    assert refresh.method == "PATCH"
    assert "status" not in refresh.body["properties"]
    assert "verdict" not in refresh.body["properties"]
    assert refresh.body["properties"]["logUri"] == "s3://s2-bench-logs/TR-HILS-0058/"


def test_properties_the_bench_left_out_are_not_sent(bench_document):
    """A PUT replaces the object, so an absent property must be absent, not null."""
    del bench_document["run"]["bench"]
    del bench_document["run"]["logUri"]
    properties = bench.plan(bench.parse(bench_document))[0].body["properties"]
    assert "bench" not in properties
    assert "logUri" not in properties


def test_the_anomaly_window_reaches_the_action():
    raised = bench.plan(bench.load(EXAMPLE))[5].body["parameters"]
    assert raised["detectedAt"] == "2026-11-19T19:02:18Z"
    assert raised["durationS"] == pytest.approx(6.6)
    assert raised["testRunId"] == "TR-HILS-0058"


def test_every_action_says_where_it_came_from():
    """The rationale reaches the approver and the audit log."""
    actions = [call for call in bench.plan(bench.load(EXAMPLE)) if "/actions/" in call.path]
    assert actions
    for call in actions:
        assert "bench report TR-HILS-0058 from HILS bench 2" in call.body["rationale"]


def test_an_unattributed_anomaly_is_left_open():
    """The second anomaly names no subsystem, so nothing confirms it."""
    attributions = [c for c in bench.plan(bench.load(EXAMPLE)) if "attributeAnomaly" in c.path]
    assert len(attributions) == 1
    assert attributions[0].body["parameters"]["anomalyId"] == "ANM-0058-01"


def test_the_verdict_is_recorded_last():
    calls = bench.plan(bench.load(EXAMPLE))
    assert "recordTestVerdict" in calls[-1].path
    assert calls[-1].body["parameters"]["verdict"] == "FAIL"


def test_a_corrected_verdict_is_not_a_replay(bench_document):
    """The key carries the verdict, or a bench could never correct one."""
    failed = bench.plan(bench.parse(bench_document))[-1].idempotency_key
    bench_document["run"]["verdict"] = "PASS"
    passed = bench.plan(bench.parse(bench_document))[-1].idempotency_key
    assert failed != passed


def test_raising_the_same_anomaly_twice_uses_the_same_key():
    first = bench.plan(bench.load(EXAMPLE))[5]
    assert first.idempotency_key == bench.plan(bench.load(EXAMPLE))[5].idempotency_key
    assert first.tolerate_409 is True


def test_a_run_with_no_build_or_drone_links_nothing(bench_document):
    del bench_document["run"]["buildId"]
    del bench_document["run"]["droneKey"]
    bench_document["run"]["verifies"] = []
    assert [path for _, _, path in steps(bench.parse(bench_document)) if "/links/" in path] == []
