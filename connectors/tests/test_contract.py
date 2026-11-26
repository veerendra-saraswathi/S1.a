"""What S1.a's gateways publish, against telemetry contract v1 in full.

contracts/telemetry/v1/ is a pinned copy of S2's frozen contract (`make check-contract`
compares the two). The gateways' Go tests check their types and patterns against it, but
Go's standard library cannot validate JSON Schema. So this runs the gateways' tests with
S2_CONTRACT_OUT set, which makes each test write every message it would publish
(gateways/internal/s2/s2test), and validates each one here: topic and payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
V1 = ROOT / "contracts" / "telemetry" / "v1"
MESSAGE = jsonschema.Draft202012Validator(json.loads((V1 / "message.schema.json").read_text()))
TOPIC = jsonschema.Draft202012Validator(json.loads((V1 / "topic.schema.json").read_text()))
CASES = json.loads((V1 / "cases.json").read_text())


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict]]:
    """Every message the gateways' tests published, by package."""
    if shutil.which("go") is None:
        pytest.skip("go is not installed: the gateways' output cannot be produced")
    out = tmp_path_factory.mktemp("contract")
    run = subprocess.run(
        ["go", "test", "-count=1", "./..."],
        cwd=ROOT / "gateways",
        env={**os.environ, "S2_CONTRACT_OUT": str(out)},
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.fail(f"the gateways' tests failed, so their output was not checked:\n{run.stdout[-3000:]}")
    return {f.stem: [json.loads(line) for line in f.read_text().splitlines()] for f in out.glob("*.jsonl")}


def test_the_pinned_copy_agrees_with_itself() -> None:
    """The schemas give every case its verdict, as S2's own conformance test requires."""
    for case in CASES["topics"]:
        assert TOPIC.is_valid(case["topic"]) is case["valid"], case["why"]
    for case in CASES["messages"]:
        if "payload_text" in case:
            continue  # not JSON: nothing for a schema to judge
        expected = True if case.get("schema") == "form-only" else case["valid"]
        assert MESSAGE.is_valid(case["payload"]) is expected, case["why"]


def test_every_gateway_publishes(published: dict[str, list[dict]]) -> None:
    assert set(published) >= {
        "mavlink-gateway-internal-telemetry",  # the aggregator, every clock case
        "mavlink-gateway-cmd-mavlink-gateway",  # the receive loop, end to end
        "can-gateway-internal-bus",  # DroneCAN from a capture
    }


def test_everything_published_is_v1(published: dict[str, list[dict]]) -> None:
    failures = []
    for package, lines in published.items():
        for line in lines:
            errors = [f"topic: {e.message}" for e in TOPIC.iter_errors(line["topic"])]
            errors += [
                f"{'/'.join(map(str, e.path)) or 'payload'}: {e.message}"
                for e in MESSAGE.iter_errors(line["payload"])
            ]
            failures += [f"{package} {line['test']}: {err}" for err in errors]
    assert not failures, "\n".join(failures)


def test_the_output_covers_every_clock_source(published: dict[str, list[dict]]) -> None:
    """A check of nothing passes too: hold the output to the cases that matter."""
    lines = [line for package in published.values() for line in package]
    sources = {line["payload"]["metrics"].get("clock_source") for line in lines}
    assert sources >= {"estimated", "gateway", "gnss", "capture"}
    assert any(line["payload"].get("position") for line in published["mavlink-gateway-internal-telemetry"])
    assert len(lines) >= 20
