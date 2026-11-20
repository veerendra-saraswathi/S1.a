# can-gateway

Reads a DroneCAN bus capture and republishes it over MQTT in the S2 telemetry contract.

```
CAN bus -> candump -L -> can-gateway -> MQTT -> S2 mqtt-bridge -> Kafka -> TimescaleDB
```

It reads the capture as text from `candump -L` (Linux can-utils), so it never opens a CAN
socket and cannot write to the bus. Like the MAVLink gateway it has no dependencies
outside the Go standard library and builds to one static binary.

## Run

```bash
make build-can                                                   # bin/can-gateway

# Live, on the bench PC (Linux, with the rig on can0):
candump -L can0 | ./bin/can-gateway -nodes 10=SUB-DRN1-GIMBAL,20=SUB-DRN1-ESC

# A recorded run, uploaded after the fact at its original timestamps:
candump -L can0 > run.log                                        # on the bench
./bin/can-gateway -input run.log -nodes 10=SUB-DRN1-GIMBAL

# Without a bench: the simulated rig (tools/can-sim)
make can-dry                                                     # printed
make can-run                                                     # live, to the S2 broker
```

## Options

| Flag | Environment | Default | Meaning |
|---|---|---|---|
| `-input` | | `-` (stdin) | `candump -L` log to read |
| `-iface` | | all | Only frames from this interface, e.g. `can0` |
| `-mqtt` | `S2_MQTT_URL` | `mqtt://localhost:1883` | Broker; `mqtts://` and `user:pass@` are supported |
| `-tenant` | `S2_TENANT` | S2's local dev tenant | Tenant UUID in the topic |
| `-object-type` | `CAN_GATEWAY_OBJECT_TYPE` | `Subsystem` | S2 object type of the nodes |
| `-nodes` | `CAN_GATEWAY_NODES` | none | `10=SUB-DRN1-GIMBAL,20=SUB-DRN1-ESC`; unmapped nodes publish as `CAN-<id>` |
| `-actuators` | `CAN_GATEWAY_ACTUATORS` | none | `3=SUB-DRN1-GIMBAL`: files an actuator under an object other than its node's |
| `-rate-hz` | | `10` | Messages per object per second of capture, up to 100 |
| `-client-id` | `CAN_GATEWAY_CLIENT_ID` | `can-gateway` | MQTT client id; must be unique per gateway |
| `-dry-run` | | off | Print `topic payload` lines, do not connect |

## What it publishes

Topic: `s2/v1/{tenant}/{ObjectType}/{key}/telemetry`, one object per node.

| DroneCAN message | Metrics |
|---|---|
| `uavcan.protocol.NodeStatus` (341) | `node_health` (OK, WARNING, ERROR, CRITICAL), `node_mode`, `node_uptime_s`, `node_vendor_status` |
| `uavcan.equipment.esc.Status` (1034) | `esc_<i>_rpm`, `_voltage`, `_current_a`, `_temperature_c`, `_power_pct`, `_error_count` |
| `uavcan.equipment.actuator.Status` (1011) | `actuator_<id>_position`, `_force`, `_speed`, `_power_pct` |
| `uavcan.equipment.actuator.ArrayCommand` (1010) | `actuator_<id>_command`, `_command_type` |

**Commands are filed with the actuator they command**, not with the flight controller
that broadcast them. An actuator's object is the one named for it with `-actuators`, or
else the object of the node that reports its status, so commanded and measured position
land on the same object. That pair is what an actuator fault twin (M6) compares. A
command for an actuator nobody has reported yet is counted as unattributed, not guessed.

Values DroneCAN marks unmeasured (NaN force or speed) are left out. Every message carries
`clock_source: capture`.

## Timestamps and rate

`ts` is the **capture time** of the newest frame in the message: when the capturing
host's kernel received it, as `candump -L` records it. Messages are cut on capture time
too: one per object per `1/rate-hz` of the capture, so replaying a log always produces
the same messages, stamped as they happened. On a live capture a window is published
when the next frame arrives; every DroneCAN node sends NodeStatus at least once a second,
so a working bus never holds one back.

To align the bus with other bench signals to better than a millisecond, the capturing
host's clock must be PTP-disciplined. The gateway records whatever that clock said.

## The transport

DroneCAN messages travel in 29-bit extended frames. The gateway reassembles multi-frame
transfers by source node and data type, checks the alternating toggle bit and transfer
id, and verifies each transfer's CRC-16 over the data type signature and payload. A
transfer with a lost frame, a bad CRC, or more than 2 s between frames is dropped and
counted. Service frames, anonymous frames and 11-bit frames are ignored; other message
types are counted as undecoded. The counters are logged every 30 s and at the end.

## Known limits

- **Linux capture only.** Live input is `candump` on Linux SocketCAN. On macOS, the gateway
  runs on recorded logs and on the simulator; the live path has not been run on a real bus.
- **Four message types are decoded.** Others are counted, not read. `esc.RawCommand` and
  DroneCAN's GlobalTimeSync are the likely next ones.
- **Classic CAN only.** CAN FD frames are counted as unsupported, and so are remote frames.
- **QoS 0.** A message published while the broker is down is lost; on a recorded log,
  re-run the upload.
- **`ts` is per message, not per metric.** At the default 10 Hz a message's values can be
  up to 100 ms apart. Use `-rate-hz 50` for a 50 Hz control loop.
- **Throughput is not benchmarked.** A three-node rig at 50 Hz has been run end to end.

## Adding a message

1. Add its data type id and DSDL signature to `signatures` in `internal/dronecan/messages.go`.
2. Write a decoder with the bit reader. Fields are read in DSDL declaration order.
3. Add a case to `tools/can-sim/gen_golden.py`, run `make can-golden`, and assert the
   decoded values in `internal/dronecan/dronecan_test.go`.
4. Map it to metrics in `internal/bus/bus.go`. Metric names are `snake_case`.
