"""The build gate: which documents ask for a step, and what a refusal does."""

from __future__ import annotations

import httpx
import pytest

from s2_connectors import bench, build
from s2_connectors.submit import submit


def gate_calls(calls):
    return [call for call in calls if "advanceBuild" in call.path]


class Engine:
    """Answers 200 to everything except the gate, which answers what it is told to."""

    def __init__(self, gate_status=200, gate_body=None):
        self.gate = (gate_status, gate_body or {})
        self.sent = []

    def request(self, user, method, path, body=None, idempotency_key=None, extra_headers=None):
        self.sent.append(path)
        status, payload = self.gate if "advanceBuild" in path else (200, {})
        return httpx.Response(status, json=payload, request=httpx.Request(method, f"http://engine{path}"))


# --- the build record: the simulation step ---------------------------------------


def test_a_passed_sitl_run_asks_for_the_simulation_step(build_document):
    (step,) = gate_calls(build.plan(build.parse(build_document)))
    assert step.user == "operator"
    assert step.body["parameters"] == {
        "buildId": "BLD-FC-2026.11.4",
        "stage": "SIM_PASSED",
        "testRunId": "TR-SITL-2026.11.4",
        "note": "TR-SITL-2026.11.4 passed.",
    }


def test_the_step_is_asked_for_after_every_verdict(build_document):
    """The gate is only asked about runs whose verdicts are already on record."""
    calls = build.plan(build.parse(build_document))
    assert "advanceBuild" in calls[-1].path
    assert sum("recordTestVerdict" in call.path for call in calls[:-1]) == 2


def test_the_step_names_the_pipeline_run(build_document):
    (step,) = gate_calls(build.plan(build.parse(build_document)))
    assert "offline-ci/flight-software/4821" in step.body["rationale"]


def test_unit_tests_alone_earn_nothing(build_document):
    build_document["testRuns"] = build_document["testRuns"][:1]
    assert gate_calls(build.plan(build.parse(build_document))) == []


def test_a_failed_sitl_run_earns_nothing(build_document):
    build_document["testRuns"][1]["verdict"] = "FAIL"
    assert gate_calls(build.plan(build.parse(build_document))) == []


def test_a_failing_unit_run_holds_the_build_even_when_sitl_passed(build_document):
    """A build has not passed the pipeline while anything in its record failed."""
    build_document["testRuns"][0]["verdict"] = "FAIL"
    assert gate_calls(build.plan(build.parse(build_document))) == []


def test_an_inconclusive_run_holds_the_build(build_document):
    build_document["testRuns"][0]["verdict"] = "INCONCLUSIVE"
    assert gate_calls(build.plan(build.parse(build_document))) == []


def test_two_passed_simulation_runs_ask_once(build_document):
    second = dict(build_document["testRuns"][1], testRunId="TR-SIM-2026.11.4", kind="SIMULATION")
    build_document["testRuns"].append(second)
    (step,) = gate_calls(build.plan(build.parse(build_document)))
    assert step.body["parameters"]["testRunId"] == "TR-SITL-2026.11.4"


# --- the bench report: the bench step -------------------------------------------


def test_a_passed_hils_run_asks_for_the_bench_step(bench_pass_document):
    calls = bench.plan(bench.parse(bench_pass_document))
    (step,) = gate_calls(calls)
    assert calls[-1] is step
    assert step.body["parameters"]["stage"] == "BENCH_PASSED"
    assert step.body["parameters"]["buildId"] == "BLD-FC-2026.11.4"
    assert step.body["parameters"]["testRunId"] == "TR-HILS-0059"
    assert "HILS bench 2" in step.body["rationale"]


def test_a_failed_hils_run_earns_nothing(bench_document):
    assert bench_document["run"]["verdict"] == "FAIL"
    assert gate_calls(bench.plan(bench.parse(bench_document))) == []


def test_a_run_against_no_build_earns_nothing(bench_pass_document):
    del bench_pass_document["run"]["buildId"]
    assert gate_calls(bench.plan(bench.parse(bench_pass_document))) == []


@pytest.mark.parametrize("kind", ["ACTUATOR_BENCH", "FLIGHT", "UNIT"])
def test_other_kinds_earn_nothing(bench_pass_document, kind):
    """An actuator rig exercises a part, not the build; a flight comes after clearance."""
    bench_pass_document["run"]["kind"] = kind
    assert gate_calls(bench.plan(bench.parse(bench_pass_document))) == []


def test_a_simulation_reported_by_a_bench_earns_the_simulation_step(bench_pass_document):
    bench_pass_document["run"]["kind"] = "SIMULATION"
    (step,) = gate_calls(bench.plan(bench.parse(bench_pass_document)))
    assert step.body["parameters"]["stage"] == "SIM_PASSED"


# --- re-submission and refusal ----------------------------------------------------


def test_the_key_separates_builds_stages_and_runs(bench_pass_document):
    (step,) = gate_calls(bench.plan(bench.parse(bench_pass_document)))
    assert step.idempotency_key == "BLD-FC-2026.11.4:advanceBuild:BENCH_PASSED:TR-HILS-0059"


def test_a_held_gate_is_reported_in_the_policys_words(bench_pass_document):
    """A HILS pass on a build that skipped simulation: the verdict stands, the step does not."""
    reason = "a build reaches BENCH_PASSED from SIM_PASSED, and this one is BUILT"
    engine = Engine(403, {"detail": {"message": "action denied by policy", "reasons": [reason]}})
    logged = []
    submit(engine, bench.plan(bench.parse(bench_pass_document)), log=logged.append)
    assert any("recordTestVerdict" in path for path in engine.sent)
    assert logged[-1].startswith("403 ")
    assert "gate held" in logged[-1]
    assert reason in logged[-1]


def test_a_denial_without_a_reason_points_at_the_policy(bench_pass_document):
    """What an OPA that is not serving build_gate.rego looks like from here."""
    detail = {"message": "action denied by policy", "reasons": ["not allowed by policy"]}
    engine = Engine(403, {"detail": detail})
    logged = []
    submit(engine, bench.plan(bench.parse(bench_pass_document)), log=logged.append)
    assert "make install-policy" in logged[-1]


def test_a_refusal_that_is_not_json_is_still_reported(bench_pass_document):
    class Plain(Engine):
        def request(self, user, method, path, body=None, idempotency_key=None, extra_headers=None):
            if "advanceBuild" in path:
                return httpx.Response(403, text="Forbidden", request=httpx.Request(method, f"http://engine{path}"))
            return super().request(user, method, path, body, idempotency_key, extra_headers)

    logged = []
    submit(Plain(), bench.plan(bench.parse(bench_pass_document)), log=logged.append)
    assert "Forbidden" in logged[-1]
