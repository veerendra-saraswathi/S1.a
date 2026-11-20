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

step(status, stage) := {
	"stage": "request",
	"actor": operator,
	"requested_by": operator.id,
	"action": advance,
	"parameters": {"buildId": "BLD-FC-2026.11.4", "stage": stage, "testRunId": "TR-1"},
	"target": build(status),
}

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
