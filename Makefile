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
#
# `make help` lists everything.

GATEWAY_DIR := gateways/mavlink-gateway
BIN         := bin/mavlink-gateway
GOLDEN      := $(GATEWAY_DIR)/internal/mavlink/testdata/golden.txt
SIM         := tools/mavlink-sim/sim.py
SEED        := tools/seed-chain/seed.py

# Override on the command line, e.g. make sim DRONES=5
VEHICLES ?= 1=DRN-0001,2=DRN-0002,3=DRN-0003
DRONES   ?= 3
RATE_HZ  ?= 1

.DEFAULT_GOAL := help
.PHONY: help test build run run-dry sim seed seed-dry golden lint fmt clean

help:  ## list the targets
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| sed -e 's/:[^#]*## /|/' \
		| awk -F'|' '{printf "  %-9s %s\n", $$1, $$2}'

test:  ## run the parser and mapping tests
	cd $(GATEWAY_DIR) && go test ./...

build:  ## compile the gateway to bin/mavlink-gateway
	@mkdir -p bin
	cd $(GATEWAY_DIR) && go build -o $(CURDIR)/$(BIN) ./cmd/mavlink-gateway

run-dry: build  ## print what would be published, connecting to nothing
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ) -dry-run

run: build  ## publish telemetry for DRN-0001..3 to the S2 broker
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ)

sim:  ## send MAVLink from simulated drones to the gateway
	uv run $(SIM) --drones $(DRONES)

seed:  ## apply the ontology pack and load the worked example into S2
	uv run $(SEED)

seed-dry:  ## print the seed requests without sending them
	uv run $(SEED) --dry-run

golden:  ## regenerate the golden frames (overwrites the test fixture)
	uv run tools/mavlink-sim/gen_golden.py > $(GOLDEN)

lint:  ## vet the Go code and lint the Python tools
	cd $(GATEWAY_DIR) && go vet ./...
	uvx ruff check tools

fmt:  ## gofmt the gateway
	cd $(GATEWAY_DIR) && gofmt -w .

clean:  ## remove build output
	rm -rf bin
