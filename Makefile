# S1.a task entry points.
#
# The gateway needs Go 1.22 or later. The Python tools declare their own
# dependencies inline (PEP 723) and run under uv, so there is nothing to
# activate. See README.md.
#
#   make test        parser, mapping and telemetry contract tests
#   make check-contract  the pinned telemetry contract v1 is S2's (needs an S2 checkout)
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
CONTRACT    := contracts/telemetry/v1

# An S2 checkout, for the policy S2's OPA has to serve. Override: make seed S2_DIR=~/src/S2
S2_DIR ?= ../S2

# The S2 broker requires a login and, from outside its machine, TLS. These defaults use
# the dev certificate and passwords S2's `make up` writes into its checkout; the gateways
# and tools read them from the environment. Override any of them, e.g. for another broker:
#   make run S2_MQTT_URL=mqtts://broker.example:8883 S2_MQTT_PASSWORD_FILE=~/gw.password
MQTT_SECRETS ?= $(S2_DIR)/deploy/docker-compose/mosquitto/secrets
S2_MQTT_URL ?= mqtts://localhost:8883
S2_MQTT_USERNAME ?= gw-dev
S2_MQTT_PASSWORD_FILE ?= $(abspath $(MQTT_SECRETS)/gw-dev.password)
S2_MQTT_CA_FILE ?= $(abspath $(MQTT_SECRETS)/ca.crt)
S2_MQTT_MONITOR_PASSWORD_FILE ?= $(abspath $(MQTT_SECRETS)/s2-monitor.password)
export S2_MQTT_URL S2_MQTT_USERNAME S2_MQTT_PASSWORD_FILE S2_MQTT_CA_FILE S2_MQTT_MONITOR_PASSWORD_FILE

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
# A small SITL fleet: copter i has system id i, publishes as DRN-000i, has its home 180 m
# north of copter i-1's, and its control port at 5762 + 10 * (i - 1).
FLEET ?= 3
REPORT   ?= $(CONNECTORS)/examples/TR-HILS-0058.json
RECORD   ?= $(CONNECTORS)/examples/BLD-FC-2026.11.4.json

.DEFAULT_GOAL := help
.PHONY: help test test-gateway test-gateway-linux test-connectors check-contract test-policy install-policy build build-can run run-dry \
        sim sitl-image sitl sitl-gw sitl-fly sitl-stop sitl-fleet sitl-fleet-gw sitl-fleet-fly sitl-fleet-stop pipeline pipeline-dry can-dry can-run can-golden bench bench-go seed seed-dry submit-bench submit-bench-dry submit-build submit-build-dry gate \
        golden lint fmt clean

help:  ## list the targets
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) \
		| sed -e 's/:[^#]*## /|/' \
		| awk -F'|' '{printf "  %-15s %s\n", $$1, $$2}'

test: test-gateway test-connectors  ## run every test

test-gateway:  ## both gateways' parser, decoder and mapping tests
	cd $(GATEWAYS) && go test ./...

test-gateway-linux:  ## the gateways' tests on Linux, in Docker (pulls golang:1.27-bookworm once, about 1.2 GB)
	docker run --rm -v $(CURDIR)/$(GATEWAYS):/src:ro -w /src -e GOCACHE=/tmp/gocache -e GOFLAGS=-buildvcs=false \
		golang:1.27-bookworm go test -count=1 -race ./...

test-connectors:  ## the connectors' tests, and the gateways' output against telemetry contract v1
	cd $(CONNECTORS) && uv run --quiet pytest -q

check-contract:  ## the pinned telemetry contract v1 is S2's, byte for byte (needs an S2 checkout)
	@test -d $(S2_DIR)/$(CONTRACT) || { echo "no $(CONTRACT) in an S2 checkout at $(S2_DIR); set S2_DIR"; exit 1; }
	@diff -r $(CONTRACT) $(S2_DIR)/$(CONTRACT) && echo "$(CONTRACT) matches $(S2_DIR)" \
		|| { echo "the pinned contract differs from S2's: v1 is frozen, so one side is wrong (see $(CONTRACT)/README.md)"; exit 1; }

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
	docker run -d --rm --name s1a-sitl --add-host=host.docker.internal:host-gateway -p 127.0.0.1:5762:5762 -p 127.0.0.1:5763:5763 -e SYSID=$(SITL_SYSID) $(SITL_IMAGE)
	@echo "SITL up. Next: make sitl-gw (publish to S2), then make sitl-fly. Stop: make sitl-stop"

sitl-gw: build  ## the gateway for SITL at 10 Hz, publishing as SITL_VEHICLE (default DRN-0001)
	./$(BIN) -vehicles $(SITL_SYSID)=$(SITL_VEHICLE) -rate-hz 10

sitl-fly:  ## fly the SITL copter: take off, a 100 m square, RTL
	uv run tools/sitl/fly.py

pipeline:  ## build the gateway, test it, fly it through SITL into S2, and submit the build record
	uv run tools/pipeline/pipeline.py --submit

pipeline-dry:  ## the same without S2 or submitting: SITL judged on the gateway's printed output
	uv run tools/pipeline/pipeline.py --sitl dry

sitl-stop:  ## stop the SITL container
	-docker rm -f s1a-sitl

sitl-fleet: sitl-image  ## start FLEET ArduCopter SITL containers (default 3, at most 5), all streaming to UDP 14550
	@test $(FLEET) -ge 1 -a $(FLEET) -le 5 || { echo "FLEET must be 1 to 5"; exit 1; }
	@if docker ps -q --filter name=^s1a-sitl$$ | grep -q .; then echo "make sitl-stop first: the single SITL uses copter 1's ports"; exit 1; fi
	@for i in $$(seq 1 $(FLEET)); do \
		lat=$$(awk "BEGIN { printf \"%.6f\", 17.4401 + 0.0016 * ($$i - 1) }"); \
		docker run -d --rm --name s1a-sitl-$$i --add-host=host.docker.internal:host-gateway \
			-p 127.0.0.1:$$((5752 + 10 * i)):5762 -p 127.0.0.1:$$((5753 + 10 * i)):5763 \
			-e SYSID=$$i -e SITL_HOME=$$lat,78.3489,540,0 $(SITL_IMAGE) >/dev/null || exit 1; \
		echo "copter $$i: system id $$i, home $$lat,78.3489, control tcp:127.0.0.1:$$((5752 + 10 * i))"; \
	done
	@echo "Next: make sitl-fleet-gw (publish to S2), then make sitl-fleet-fly. Stop: make sitl-fleet-stop"

sitl-fleet-gw: build  ## one gateway for the fleet at 10 Hz: system id i publishes as DRN-000i
	./$(BIN) -vehicles $$(seq 1 $(FLEET) | awk '{ printf "%s%d=DRN-%04d", (NR > 1 ? "," : ""), $$1, $$1 }') -rate-hz 10 -client-id s1a-fleet-gw

sitl-fleet-fly:  ## fly the fleet at once, reboot copter 2 after landing, and check every drone in S2
	uv run tools/sitl/fleet.py --fleet $(FLEET) --reboot 2

sitl-fleet-stop:  ## stop the fleet's SITL containers, s1a-sitl-1 to s1a-sitl-5
	-@for i in 1 2 3 4 5; do docker rm -f s1a-sitl-$$i >/dev/null 2>&1 || true; done

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
