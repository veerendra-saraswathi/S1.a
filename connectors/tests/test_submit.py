"""How a plan is sent: what is tolerated, what stops, and what is reported."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from s2_connectors import bench
from s2_connectors.client import S2Error
from s2_connectors.submit import submit

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "TR-HILS-0058.json"


class FakeClient:
    """Answers what each call is told to answer, and records what it was sent."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sent = []

    def request(self, user, method, path, body=None, idempotency_key=None, extra_headers=None):
        self.sent.append((user, method, path, idempotency_key, extra_headers))
        key = path.rsplit("/", 2)[-2] if "/actions/" in path else path
        status, headers = self.answers.get(key, (200, {}))
        return httpx.Response(
            status_code=status,
            headers=headers,
            text="{}",
            request=httpx.Request(method, f"http://engine{path}"),
        )


def plan():
    return bench.plan(bench.load(EXAMPLE))


def test_every_call_is_sent_in_order():
    client = FakeClient()
    submit(client, plan(), log=lambda _: None)
    assert len(client.sent) == 8
    assert client.sent[0][1] == "PUT"
    assert "recordTestVerdict" in client.sent[-1][2]


def test_an_already_recorded_anomaly_does_not_stop_the_submission():
    """409 on raiseAnomaly is a re-submitted report, not a failure."""
    logged = []
    client = FakeClient({"raiseAnomaly": (409, {})})
    submit(client, plan(), log=logged.append)
    assert len(client.sent) == 8
    assert any("409" in line and "unchanged" in line for line in logged)


def test_a_refusal_stops_the_submission():
    client = FakeClient({"/v1/objects/TestRun/TR-HILS-0058": (403, {})})
    with pytest.raises(S2Error, match="403"):
        submit(client, plan(), log=lambda _: None)
    assert len(client.sent) == 1


def test_a_missing_reference_is_explained():
    client = FakeClient({"/v1/objects/TestRun/TR-HILS-0058/links/testsBuild": (404, {})})
    with pytest.raises(S2Error, match="not in S2 yet"):
        submit(client, plan(), log=lambda _: None)


def test_an_awaiting_approval_answer_is_reported():
    logged = []
    submit(FakeClient({"recordTestVerdict": (202, {})}), plan(), log=logged.append)
    assert any("awaiting approval" in line for line in logged)


def test_a_replayed_answer_is_reported():
    logged = []
    client = FakeClient({"recordTestVerdict": (200, {"Idempotent-Replay": "true"})})
    submit(client, plan(), log=logged.append)
    assert any("(replay)" in line for line in logged)


def test_extra_headers_reach_the_client():
    """The build connector guards its create with If-None-Match."""
    from s2_connectors import build

    record = build.load(Path(EXAMPLE).parent / "BLD-FC-2026.11.4.json")
    client = FakeClient()
    submit(client, build.plan(record), log=lambda _: None)
    assert client.sent[0][4] == {"If-None-Match": "*"}
