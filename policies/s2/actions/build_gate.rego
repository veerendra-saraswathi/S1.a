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
# What this cannot see: the engine shows a policy the build, not the run named as
# evidence. That the run passed and tested this build is the requester's claim, kept in
# the audit log beside the decision.
#
# This file is served by S2's OPA. `make install-policy` copies it into an S2 checkout.
package s2.actions.build_gate

import rego.v1

import data.s2.actions.common

# The status a build must hold for each step to be taken.
needs := {"SIM_PASSED": "BUILT", "BENCH_PASSED": "SIM_PASSED"}

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
