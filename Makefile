# S1.a task entry points.
#
# The gateway needs Go 1.22 or later. The Python tools declare their own
# dependencies inline (PEP 723) and run under uv, so there is nothing to
# activate. See README.md.
#
#   make test        parser and mapping tests
#   make run-dry     the gateway prints what it would publish
#   make sim         three simulated drones
#   make can-dry     a simulated DroneCAN bench through the CAN gateway, printed
#   make can-run     the same bench, live, published to the S2 broker
#   make run         publish telemetry to the S2 broker
#   make seed        apply the ontology pack and load the worked example
#   make gate        walk a build through the gate, from record to bench pass
#   make bench       end-to-end latency per hop at increasing load (needs S2 up)
#   make sitl        ArduCopter SITL streaming to the gateway; make sitl-gw, make sitl-fly
#
# `make help` lists everything.

GATEWAYS    := gateways
BIN         := bin/mavlink-gateway
CAN_BIN     := bin/can-gateway
GOLDEN      := $(GATEWAYS)/mavlink-gateway/internal/mavlink/testdata/golden.txt
CAN_GOLDEN  := $(GATEWAYS)/can-gateway/internal/dronecan/testdata/golden.log
SIM         := tools/mavlink-sim/sim.py
CAN_SIM     := tools/can-sim/sim.py
CONNECTORS  := connectors
SEED        := tools/seed-chain/seed.py
POLICY      := policies/s2/actions/build_gate.rego

# An S2 checkout, for the policy S2's OPA has to serve. Override: make seed S2_DIR=~/src/S2
S2_DIR ?= ../S2

# Override on the command line, e.g. make sim DRONES=5
VEHICLES ?= 1=DRN-0001,2=DRN-0002,3=DRN-0003
DRONES   ?= 3
RATE_HZ  ?= 1
# The simulated bench's DroneCAN nodes: flight controller, gimbal, ESC board.
CAN_NODES ?= 1=SUB-DRN1-FC,10=SUB-DRN1-GIMBAL,20=SUB-DRN1-ESC
CAN_FAULT ?= reduced_gain
STEPS     ?= 3,10,50
# ArduCopter SITL: its MAVLink system id and the S2 object it publishes as.
SITL_IMAGE   := s1a-sitl-copter
SITL_SYSID   ?= 1
SITL_VEHICLE ?= DRN-0001
REPORT   ?= $(CONNECTORS)/examples/TR-HILS-0058.json
RECORD   ?= $(CONNECTORS)/examples/BLD-FC-2026.11.4.json

.DEFAULT_GOAL := help
.PHONY: help test test-gateway test-connectors test-policy install-policy build build-can run run-dry \
        sim sitl-image sitl sitl-gw sitl-fly sitl-stop can-dry can-run can-golden bench bench-go seed seed-dry submit-bench submit-bench-dry submit-build submit-build-dry gate \
        golden lint fmt clean

help:  ## list the targets
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| sed -e 's/:[^#]*## /|/' \
		| awk -F'|' '{printf "  %-15s %s\n", $$1, $$2}'

test: test-gateway test-connectors  ## run every test

test-gateway:  ## both gateways' parser, decoder and mapping tests
	cd $(GATEWAYS) && go test ./...

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

build:  ## compile the MAVLink gateway to bin/mavlink-gateway
	@mkdir -p bin
	cd $(GATEWAYS) && go build -o $(CURDIR)/$(BIN) ./mavlink-gateway/cmd/mavlink-gateway

build-can:  ## compile the CAN gateway to bin/can-gateway
	@mkdir -p bin
	cd $(GATEWAYS) && go build -o $(CURDIR)/$(CAN_BIN) ./can-gateway/cmd/can-gateway

run-dry: build  ## print what would be published, connecting to nothing
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ) -dry-run

run: build  ## publish telemetry for DRN-0001..3 to the S2 broker
	./$(BIN) -vehicles $(VEHICLES) -rate-hz $(RATE_HZ)

sim:  ## send MAVLink from simulated drones to the gateway
	uv run $(SIM) --drones $(DRONES)

sitl-image:  ## build the ArduCopter SITL image (downloads the pinned stable binary once)
	docker build -t $(SITL_IMAGE) tools/sitl

sitl: sitl-image  ## start ArduCopter SITL in Docker, streaming MAVLink to UDP 14550 on this host
	docker run -d --rm --name s1a-sitl -p 127.0.0.1:5762:5762 -p 127.0.0.1:5763:5763 -e SYSID=$(SITL_SYSID) $(SITL_IMAGE)
	@echo "SITL up. Next: make sitl-gw (publish to S2), then make sitl-fly. Stop: make sitl-stop"

sitl-gw: build  ## the gateway for SITL at 10 Hz, publishing as SITL_VEHICLE (default DRN-0001)
	./$(BIN) -vehicles $(SITL_SYSID)=$(SITL_VEHICLE) -rate-hz 10

sitl-fly:  ## fly the SITL copter: take off, a 100 m square, RTL
	uv run tools/sitl/fly.py

sitl-stop:  ## stop the SITL container
	-docker rm -f s1a-sitl

can-dry: build-can  ## 20 s of a simulated DroneCAN bench, with a fault at 10 s, printed (CAN_FAULT=...)
	uv run $(CAN_SIM) --seconds 20 --fault $(CAN_FAULT) --fault-at 10 | ./$(CAN_BIN) -nodes $(CAN_NODES) -dry-run

can-run: build-can  ## the simulated bench in real time, published to the S2 broker (Ctrl-C to stop)
	uv run $(CAN_SIM) --realtime --seconds 0 --fault $(CAN_FAULT) --fault-at 10 | ./$(CAN_BIN) -nodes $(CAN_NODES)

bench: build  ## end-to-end latency per hop, 3 to 50 simulated drones (STEPS=3,10,50; needs S2 up, a quiet machine)
	uv run tools/bench/bench.py --steps $(STEPS) --seconds 20

bench-go:  ## the gateways' receive-path benchmarks
	cd $(GATEWAYS) && go test -run '^$$' -bench . -benchmem ./mavlink-gateway/internal/telemetry ./can-gateway/internal/bus

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

golden:  ## regenerate the MAVLink golden frames (overwrites the test fixture)
	uv run tools/mavlink-sim/gen_golden.py > $(GOLDEN)

can-golden:  ## regenerate the DroneCAN golden frames (overwrites the test fixture)
	uv run tools/can-sim/gen_golden.py > $(CAN_GOLDEN)

lint:  ## vet the Go code and lint the Python
	cd $(GATEWAYS) && go vet ./...
	uvx ruff check tools
	cd $(CONNECTORS) && uvx ruff check .

fmt:  ## gofmt the gateways
	cd $(GATEWAYS) && gofmt -w .

clean:  ## remove build output
	rm -rf bin
