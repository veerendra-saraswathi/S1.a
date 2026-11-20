# Policy for the build gate (ontologies/drone-engineering): advanceBuild and
# clearBuildForFlight.
#
# A build earns flight clearance in order:
#
#   BUILT -> SIM_PASSED -> BENCH_PASSED -> CLEARED_FOR_FLIGHT
#
# advanceBuild takes one step at a time and never skips or goes back.
# clearBuildForFlight may only be asked of, and approved for, a BENCH_PASSED build. The
# engine decides again at approval, so a build that was rejected while the request
# waited cannot be cleared by a late approval.
#
# A step is earned by the run it names, which the action type puts in front of the
# policy as input.context.run (S2's policy_context). The run must exist, have tested this
# build, be COMPLETED with verdict PASS, and be of a kind that earns the step: SITL or
# SIMULATION for SIM_PASSED, HILS for BENCH_PASSED.
#
# This file is served by S2's OPA. `make install-policy` copies it into an S2 checkout.
package s2.actions.build_gate

import rego.v1

import data.s2.actions.common

# The status a build must hold for each step to be taken.
needs := {"SIM_PASSED": "BUILT", "BENCH_PASSED": "SIM_PASSED"}

# The kinds of run that earn each step.
earned_by := {"SIM_PASSED": {"SITL", "SIMULATION"}, "BENCH_PASSED": {"HILS"}}

advancing if input.action.api_name == "advanceBuild"

clearing if input.action.api_name == "clearBuildForFlight"

known_action if advancing

known_action if clearing

known_stage if needs[input.parameters.stage]

in_order if input.target.properties.status == needs[input.parameters.stage]

bench_passed if input.target.properties.status == "BENCH_PASSED"

deny contains reason if some reason in common.deny

deny contains "this policy does not govern that action" if not known_action

# Holds for a null target and for a missing one.
deny contains "the build does not exist" if not input.target.primary_key

deny contains "unknown gate stage" if {
	advancing
	not known_stage
}

deny contains reason if {
	advancing
	known_stage
	not in_order
	stage := input.parameters.stage
	reason := sprintf("a build reaches %s from %s, and this one is %s", [stage, needs[stage], current])
}

# --- the evidence ---------------------------------------------------------------

run := input.context.run

run_id := input.parameters.testRunId

# Holds for a null run, a missing one, and an engine too old to send context.
run_exists if run.primary_key

run_tested_this_build if input.target.primary_key in run.links.testsBuild

run_passed if {
	run.properties.status == "COMPLETED"
	run.properties.verdict == "PASS"
}

run_kind_earns_stage if run.properties.kind in earned_by[input.parameters.stage]

deny contains sprintf("test run %s does not exist", [run_id]) if {
	advancing
	not run_exists
}

deny contains sprintf("test run %s did not test this build", [run_id]) if {
	advancing
	run_exists
	input.target.primary_key
	not run_tested_this_build
}

deny contains sprintf("test run %s has not passed: it is %s with verdict %s", [run_id, run_status, run_verdict]) if {
	advancing
	run_exists
	not run_passed
}

deny contains sprintf("a %s run does not earn %s", [run_kind, input.parameters.stage]) if {
	advancing
	run_exists
	known_stage
	not run_kind_earns_stage
}

# Printable fallbacks for the reasons above.
default run_status := "(no status)"

run_status := run.properties.status

default run_verdict := "(none)"

run_verdict := run.properties.verdict

default run_kind := "(unknown kind)"

run_kind := run.properties.kind

# --- clearance ------------------------------------------------------------------

deny contains "only a build that has passed the bench can be cleared for flight" if {
	clearing
	not bench_passed
}

# Never "(unknown)" for a build that has a status; the fallback keeps the reason
# printable when the target is missing, which the rule above already denies.
default current := "(unknown)"

current := input.target.properties.status

default allow := false

allow if {
	common.well_formed
	count(deny) == 0
}
