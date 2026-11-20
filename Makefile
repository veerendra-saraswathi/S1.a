# S1.a task entry points.
#
# The gateway needs Go 1.22 or later. The Python tools declare their own
# dependencies inline (PEP 723) and run under uv, so there is nothing to
# activate. See README.md.
#
#   make test        parser and mapping tests
#   make run-dry     the gateway prints what it would publish
#   make sim         three simulated drones
#   make run         publish telemetry to the S2 broker
#   make seed        apply the ontology pack and load the worked example
#   make gate        walk a build through the gate, from record to bench pass
#
# `make help` lists everything.

GATEWAY_DIR := gateways/mavlink-gateway
BIN         := bin/mavlink-gateway
GOLDEN      := $(GATEWAY_DIR)/internal/mavlink/testdata/golden.txt
SIM         := tools/mavlink-sim/sim.py
CONNECTORS  := connectors
SEED        := tools/seed-chain/seed.py
POLICY      := policies/s2/actions/build_gate.rego

# An S2 checkout, for the policy S2's OPA has to serve. Override: make seed S2_DIR=~/src/S2
S2_DIR ?= ../S2

# Override on the command line, e.g. make sim DRONES=5
VEHICLES ?= 1=DRN-0001,2=DRN-0002,3=DRN-0003
DRONES   ?= 3
RATE_HZ  ?= 1
REPORT   ?= $(CONNECTORS)/examples/TR-HILS-0058.json
RECORD   ?= $(CONNECTORS)/examples/BLD-FC-2026.11.4.json

.DEFAULT_GOAL := help
.PHONY: help test test-gateway test-connectors test-policy install-policy build run run-dry \
        sim seed seed-dry submit-bench submit-bench-dry submit-build submit-build-dry gate \
        golden lint fmt clean

help:  ## list the targets
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| sed -e 's/:[^#]*## /|/' \
		| awk -F'|' '{printf "  %-15s %s\n", $$1, $$2}'

test: test-gateway test-connectors  ## run every test

test-gateway:  ## the gateway's parser and mapping tests
	cd $(GATEWAY_DIR) && go test ./...

test-connectors:  ## the connectors' contract and mapping tests
	cd $(CONNECTORS) && uv run --quiet pytest -q

test-policy:  ## the build gate's policy tests (needs opa and an S2 checkout)
	@command -v opa >/dev/null || { echo "opa is not installed: https://www.openpolicyagent.org/docs#install"; exit 1; }
	@test -d $(S2_DIR)/policies || { echo "no S2 checkout at $(S2_DIR); set S2_DIR"; exit 1; }
	opa test $(S2_DIR)/policies/s2/actions/common.rego policies

install-policy:  ## copy the build gate's policy into the S2 checkout, where S2's OPA serves it
	@test -d $(S2_DIR)/policies/s2/actions || { echo "no S2 checkout at $(S2_DIR); set S2_DIR"; exit 1; }
	cp $(POLICY) $(S2_DIR)/policies/s2/actions/
	@echo "installed. S2's OPA reloads on save; if a gate request is denied with no reason, docker restart s2-opa"

build:  ## compile the gateway to bin/mavlink-gateway
	@mkdir -p bin
	cd $(GATEWAY_DIR) && go build -o $(CURDIR)/$(BIN) ./cmd/mavlink-gateway

run-dry: build  ## print what would be published, connecting to nothing
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ) -dry-run

run: build  ## publish telemetry for DRN-0001..3 to the S2 broker
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ)

sim:  ## send MAVLink from simulated drones to the gateway
	uv run $(SIM) --drones $(DRONES)

seed: install-policy  ## apply the ontology pack and load the worked example into S2
	uv run $(SEED)

seed-dry:  ## print the seed requests without sending them
	uv run $(SEED) --dry-run

submit-bench:  ## write a bench report into S2 (REPORT=path/to/report.json)
	uv run --project $(CONNECTORS) hils-submit $(REPORT)

submit-bench-dry:  ## print the calls a bench report becomes, send nothing
	uv run --project $(CONNECTORS) hils-submit $(REPORT) --dry-run

submit-build:  ## write a CI build record into S2 (RECORD=path/to/record.json)
	uv run --project $(CONNECTORS) build-submit $(RECORD)

submit-build-dry:  ## print the calls a build record becomes, send nothing
	uv run --project $(CONNECTORS) build-submit $(RECORD) --dry-run

gate:  ## walk build 2026.11.4 through the gate: recorded, simulation passed, bench passed
	uv run --project $(CONNECTORS) build-submit $(CONNECTORS)/examples/BLD-FC-2026.11.4.json
	uv run --project $(CONNECTORS) hils-submit $(CONNECTORS)/examples/TR-HILS-0059.json

golden:  ## regenerate the golden frames (overwrites the test fixture)
	uv run tools/mavlink-sim/gen_golden.py > $(GOLDEN)

lint:  ## vet the Go code and lint the Python
	cd $(GATEWAY_DIR) && go vet ./...
	uvx ruff check tools
	cd $(CONNECTORS) && uvx ruff check .

fmt:  ## gofmt the gateway
	cd $(GATEWAY_DIR) && gofmt -w .

clean:  ## remove build output
	rm -rf bin
