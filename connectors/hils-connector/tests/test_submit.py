"""What a report becomes, asserted against the plan rather than against a live engine."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from s2_hils.client import S2Error
from s2_hils.report import load, parse
from s2_hils.submit import plan, submit

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "TR-HILS-0058.json"


def steps(report):
    return [(call.user, call.method, call.path) for call in plan(report)]


def test_the_sequence_the_worked_example_becomes():
    assert steps(load(EXAMPLE)) == [
        ("admin", "PUT", "/v1/objects/TestRun/TR-HILS-0058"),
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
    put = plan(load(EXAMPLE))[0]
    assert put.body["properties"]["status"] == "RUNNING"
    assert "verdict" not in put.body["properties"]
    assert put.body["properties"]["logUri"] == "s3://s2-bench-logs/TR-HILS-0058/"


def test_properties_the_bench_left_out_are_not_sent(document):
    """A PUT replaces the object, so an absent property must be absent, not null."""
    del document["run"]["bench"]
    del document["run"]["logUri"]
    properties = plan(parse(document))[0].body["properties"]
    assert "bench" not in properties
    assert "logUri" not in properties


def test_the_anomaly_window_reaches_the_action():
    raised = plan(load(EXAMPLE))[4].body["parameters"]
    assert raised["detectedAt"] == "2026-11-19T19:02:18Z"
    assert raised["durationS"] == pytest.approx(6.6)
    assert raised["testRunId"] == "TR-HILS-0058"


def test_an_unattributed_anomaly_is_left_open():
    """The second anomaly names no subsystem, so nothing confirms it."""
    attributions = [call for call in plan(load(EXAMPLE)) if "attributeAnomaly" in call.path]
    assert len(attributions) == 1
    assert attributions[0].body["parameters"]["anomalyId"] == "ANM-0058-01"


def test_the_verdict_is_recorded_last():
    calls = plan(load(EXAMPLE))
    assert "recordTestVerdict" in calls[-1].path
    assert calls[-1].body["parameters"]["verdict"] == "FAIL"


def test_a_corrected_verdict_is_not_a_replay(document):
    """The key carries the verdict, or a bench could never correct one."""
    failed = plan(parse(document))[-1].idempotency_key
    document["run"]["verdict"] = "PASS"
    passed = plan(parse(document))[-1].idempotency_key
    assert failed != passed


def test_raising_the_same_anomaly_twice_uses_the_same_key():
    first = plan(load(EXAMPLE))[4]
    second = plan(load(EXAMPLE))[4]
    assert first.idempotency_key == second.idempotency_key
    assert first.tolerate_409 is True


def test_a_run_with_no_build_or_drone_links_nothing(document):
    del document["run"]["buildId"]
    del document["run"]["droneKey"]
    document["run"]["verifies"] = []
    assert [path for _, _, path in steps(parse(document)) if "/links/" in path] == []


class FakeClient:
    """Answers with whatever each call is told to answer, and records what it was sent."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sent = []

    def request(self, user, method, path, body=None, idempotency_key=None):
        self.sent.append((user, method, path, idempotency_key))
        status, headers = self.answers.get(path.rsplit("/", 2)[-2] if "actions" in path else path, (200, {}))
        return httpx.Response(
            status_code=status,
            headers=headers,
            text="{}",
            request=httpx.Request(method, f"http://engine{path}"),
        )


def test_submit_sends_every_call_in_order():
    client = FakeClient()
    submit(client, load(EXAMPLE), log=lambda _: None)
    assert len(client.sent) == 8
    assert client.sent[0][1] == "PUT"
    assert "recordTestVerdict" in client.sent[-1][2]


def test_an_already_recorded_anomaly_does_not_stop_the_submission():
    """409 on raiseAnomaly is a re-submitted report, not a failure."""
    logged = []
    client = FakeClient({"raiseAnomaly": (409, {})})
    submit(client, load(EXAMPLE), log=logged.append)
    assert len(client.sent) == 8
    assert any("409" in line and "unchanged" in line for line in logged)


def test_a_refusal_stops_the_submission():
    client = FakeClient({"/v1/objects/TestRun/TR-HILS-0058": (403, {})})
    with pytest.raises(S2Error, match="403"):
        submit(client, load(EXAMPLE), log=lambda _: None)
    assert len(client.sent) == 1


def test_a_missing_reference_is_explained():
    client = FakeClient({"/v1/objects/TestRun/TR-HILS-0058/links/testsBuild": (404, {})})
    with pytest.raises(S2Error, match="not in S2 yet"):
        submit(client, load(EXAMPLE), log=lambda _: None)


def test_an_awaiting_approval_answer_is_reported():
    logged = []
    client = FakeClient({"recordTestVerdict": (202, {})})
    submit(client, load(EXAMPLE), log=logged.append)
    assert any("awaiting approval" in line for line in logged)


def test_a_replayed_answer_is_reported():
    logged = []
    client = FakeClient({"recordTestVerdict": (200, {"Idempotent-Replay": "true"})})
    submit(client, load(EXAMPLE), log=logged.append)
    assert any("(replay)" in line for line in logged)
