# Run with:  make test-policy     (needs opa and an S2 checkout for the baseline rules)
package s2.actions.build_gate_test

import rego.v1

import data.s2.actions.build_gate

operator := {"id": "u-operator", "type": "user", "roles": ["operator"], "tenant_id": "t1"}

approver := {"id": "u-approver", "type": "user", "roles": ["approver"], "tenant_id": "t1"}

agent := {"id": "a-docs", "type": "agent", "roles": ["operator"], "tenant_id": "t1"}

advance := {"api_name": "advanceBuild", "version": 1, "requires_approval": false, "agent_access": "none"}

clear := {"api_name": "clearBuildForFlight", "version": 1, "requires_approval": true, "agent_access": "none"}

build(status) := {
	"object_type": "SoftwareBuild",
	"primary_key": "BLD-FC-2026.11.4",
	"properties": {"status": status},
}

# The run a step names, passed, of the kind that earns the stage.
kind_for := {"SIM_PASSED": "SITL", "BENCH_PASSED": "HILS", "CLEARED_FOR_FLIGHT": "FLIGHT"}

earning_run(kind) := {
	"object_type": "TestRun",
	"primary_key": "TR-1",
	"properties": {"testRunId": "TR-1", "kind": kind, "status": "COMPLETED", "verdict": "PASS"},
	"links": {"testsBuild": ["BLD-FC-2026.11.4"]},
}

step(status, stage) := {
	"stage": "request",
	"actor": operator,
	"requested_by": operator.id,
	"action": advance,
	"parameters": {"buildId": "BLD-FC-2026.11.4", "stage": stage, "testRunId": "TR-1"},
	"target": build(status),
	"context": {"run": earning_run(object.get(kind_for, stage, "SITL"))},
}

with_run(input_doc, patch) := json.patch(input_doc, patch)

clearance(stage, actor, status) := {
	"stage": stage,
	"actor": actor,
	"requested_by": operator.id,
	"action": clear,
	"parameters": {"buildId": "BLD-FC-2026.11.4", "justification": "All gates passed."},
	"target": build(status),
}

# --- advanceBuild ---------------------------------------------------------------

test_built_advances_to_sim_passed if build_gate.allow with input as step("BUILT", "SIM_PASSED")

test_sim_passed_advances_to_bench_passed if build_gate.allow with input as step("SIM_PASSED", "BENCH_PASSED")

test_the_bench_step_cannot_skip_simulation if {
	not build_gate.allow with input as step("BUILT", "BENCH_PASSED")
	"a build reaches BENCH_PASSED from SIM_PASSED, and this one is BUILT" in build_gate.deny with input as step("BUILT", "BENCH_PASSED")
}

test_a_step_is_not_taken_twice if not build_gate.allow with input as step("SIM_PASSED", "SIM_PASSED")

test_a_build_does_not_go_back if not build_gate.allow with input as step("BENCH_PASSED", "SIM_PASSED")

test_a_rejected_build_does_not_advance if not build_gate.allow with input as step("REJECTED", "SIM_PASSED")

test_a_cleared_build_does_not_advance if not build_gate.allow with input as step("CLEARED_FOR_FLIGHT", "BENCH_PASSED")

test_an_unknown_stage_is_denied if {
	not build_gate.allow with input as step("BUILT", "CLEARED_FOR_FLIGHT")
	"unknown gate stage" in build_gate.deny with input as step("BUILT", "CLEARED_FOR_FLIGHT")
}

test_a_missing_stage_is_denied if {
	bad := json.remove(step("BUILT", "SIM_PASSED"), ["/parameters/stage"])
	not build_gate.allow with input as bad
	"unknown gate stage" in build_gate.deny with input as bad
}

test_a_missing_build_is_denied if {
	bad := object.union(step("BUILT", "SIM_PASSED"), {"target": null})
	not build_gate.allow with input as bad
	"the build does not exist" in build_gate.deny with input as bad
}

test_a_build_without_a_status_is_denied if {
	bad := json.remove(step("BUILT", "SIM_PASSED"), ["/target/properties/status"])
	not build_gate.allow with input as bad
}

test_advancing_still_needs_the_operator_role if {
	bad := object.union(step("BUILT", "SIM_PASSED"), {"actor": approver})
	not build_gate.allow with input as bad
}

test_an_agent_cannot_advance_a_build if {
	bad := object.union(step("BUILT", "SIM_PASSED"), {"actor": agent})
	not build_gate.allow with input as bad
}

# --- the evidence a step names --------------------------------------------------

test_a_simulation_run_also_earns_sim_passed if {
	ok := with_run(step("BUILT", "SIM_PASSED"), [{"op": "replace", "path": "/context/run/properties/kind", "value": "SIMULATION"}])
	build_gate.allow with input as ok
}

test_a_failed_run_earns_nothing if {
	bad := with_run(step("SIM_PASSED", "BENCH_PASSED"), [{"op": "replace", "path": "/context/run/properties/verdict", "value": "FAIL"}])
	not build_gate.allow with input as bad
	"test run TR-1 has not passed: it is COMPLETED with verdict FAIL" in build_gate.deny with input as bad
}

test_an_inconclusive_run_earns_nothing if {
	bad := with_run(step("BUILT", "SIM_PASSED"), [{"op": "replace", "path": "/context/run/properties/verdict", "value": "INCONCLUSIVE"}])
	not build_gate.allow with input as bad
}

test_a_run_still_going_earns_nothing if {
	bad := with_run(step("BUILT", "SIM_PASSED"), [
		{"op": "replace", "path": "/context/run/properties/status", "value": "RUNNING"},
		{"op": "remove", "path": "/context/run/properties/verdict"},
	])
	not build_gate.allow with input as bad
	"test run TR-1 has not passed: it is RUNNING with verdict (none)" in build_gate.deny with input as bad
}

test_a_run_of_another_build_earns_nothing if {
	bad := with_run(step("BUILT", "SIM_PASSED"), [{"op": "replace", "path": "/context/run/links/testsBuild", "value": ["BLD-FC-2026.11.3"]}])
	not build_gate.allow with input as bad
	"test run TR-1 did not test this build" in build_gate.deny with input as bad
}

test_a_run_linked_to_no_build_earns_nothing if {
	bad := with_run(step("BUILT", "SIM_PASSED"), [{"op": "replace", "path": "/context/run/links/testsBuild", "value": []}])
	not build_gate.allow with input as bad
}

test_a_simulation_does_not_earn_the_bench if {
	bad := with_run(step("SIM_PASSED", "BENCH_PASSED"), [{"op": "replace", "path": "/context/run/properties/kind", "value": "SITL"}])
	not build_gate.allow with input as bad
	"a SITL run does not earn BENCH_PASSED" in build_gate.deny with input as bad
}

test_the_bench_does_not_earn_simulation if {
	bad := with_run(step("BUILT", "SIM_PASSED"), [{"op": "replace", "path": "/context/run/properties/kind", "value": "HILS"}])
	not build_gate.allow with input as bad
}

test_a_unit_run_earns_nothing if {
	every stage in ["SIM_PASSED", "BENCH_PASSED"] {
		bad := with_run(step(needs_for[stage], stage), [{"op": "replace", "path": "/context/run/properties/kind", "value": "UNIT"}])
		not build_gate.allow with input as bad
	}
}

needs_for := {"SIM_PASSED": "BUILT", "BENCH_PASSED": "SIM_PASSED"}

test_a_missing_run_is_denied if {
	bad := object.union(step("BUILT", "SIM_PASSED"), {"context": {"run": null}})
	not build_gate.allow with input as bad
	"test run TR-1 does not exist" in build_gate.deny with input as bad
}

test_an_engine_that_sends_no_context_is_denied if {
	bad := json.remove(step("BUILT", "SIM_PASSED"), ["/context"])
	not build_gate.allow with input as bad
	"test run TR-1 does not exist" in build_gate.deny with input as bad
}

# --- clearBuildForFlight --------------------------------------------------------

test_a_bench_passed_build_can_be_put_forward if build_gate.allow with input as clearance("request", operator, "BENCH_PASSED")

test_a_bench_passed_build_can_be_approved if build_gate.allow with input as clearance("approval", approver, "BENCH_PASSED")

test_clearance_is_refused_for_every_other_status if {
	every status in ["BUILT", "SIM_PASSED", "REJECTED", "CLEARED_FOR_FLIGHT"] {
		not build_gate.allow with input as clearance("request", operator, status)
		"only a build that has passed the bench can be cleared for flight" in build_gate.deny with input as clearance("request", operator, status)
	}
}

test_a_late_approval_cannot_clear_a_rejected_build if {
	not build_gate.allow with input as clearance("approval", approver, "REJECTED")
}

test_the_requester_still_cannot_approve if {
	not build_gate.allow with input as clearance("approval", operator, "BENCH_PASSED")
}

test_an_agent_cannot_ask_for_clearance if {
	not build_gate.allow with input as clearance("request", agent, "BENCH_PASSED")
}

# --- anything else --------------------------------------------------------------

test_another_action_is_not_governed_here if {
	other := object.union(step("BUILT", "SIM_PASSED"), {"action": object.union(advance, {"api_name": "rejectBuild"})})
	not build_gate.allow with input as other
}

test_empty_input_is_denied if not build_gate.allow with input as {}
