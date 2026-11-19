# mavlink-gateway

Listens for MAVLink on UDP and republishes it over MQTT in the S2 telemetry contract.

```
MAVLink (UDP) -> mavlink-gateway -> MQTT -> S2 mqtt-bridge -> Kafka -> TimescaleDB
```

It only listens. It never sends MAVLink, so it cannot command a vehicle. It has no dependencies outside the Go standard library and builds to a single static binary.

## Run

```bash
go build -o mavlink-gateway ./cmd/mavlink-gateway
./mavlink-gateway -vehicles 1=DRN-0001,2=DRN-0002            # publish to mqtt://localhost:1883
./mavlink-gateway -vehicles 1=DRN-0001 -dry-run              # print instead of publishing
```

Send MAVLink to UDP 14550 on the gateway host. For ArduPilot SITL: `--out udp:127.0.0.1:14550`.

## Options

| Flag | Environment | Default | Meaning |
|---|---|---|---|
| `-listen` | `GATEWAY_LISTEN` | `:14550` | UDP address for MAVLink |
| `-mqtt` | `S2_MQTT_URL` | `mqtt://localhost:1883` | Broker; `mqtts://` and `user:pass@` are supported |
| `-tenant` | `S2_TENANT` | S2's local dev tenant | Tenant UUID in the topic |
| `-object-type` | `GATEWAY_OBJECT_TYPE` | `CommercialDrone` | S2 object type |
| `-vehicles` | `GATEWAY_VEHICLES` | none | `1=DRN-0001,2=DRN-0002`; unmapped systems publish as `MAV-<id>` |
| `-rate-hz` | | `1` | Messages per vehicle per second, up to 50 |
| `-client-id` | `GATEWAY_CLIENT_ID` | `mavlink-gateway` | MQTT client id; must be unique per gateway |
| `-dry-run` | | off | Print `topic payload` lines, do not connect |

## What it publishes

Topic: `s2/v1/{tenant}/{ObjectType}/{key}/telemetry`

```json
{
  "ts": "2026-11-18T19:37:16.889Z",
  "metrics": {"armed": true, "battery_voltage": 25.098, "system_state": "ACTIVE"},
  "position": {"lat": 17.4405262, "lon": 78.3508262, "alt_m": 595, "heading_deg": 166.94, "speed_mps": 8.99},
  "seq": 1
}
```

| MAVLink message | Metrics |
|---|---|
| HEARTBEAT (0) | `armed`, `custom_mode`, `system_state` |
| SYS_STATUS (1) | `battery_voltage`, `battery_current_a`, `battery_remaining_pct`, `cpu_load_pct` |
| GPS_RAW_INT (24) | `gps_fix_type`, `gps_satellites`, `gps_hdop` |
| ATTITUDE (30) | `roll_deg`, `pitch_deg`, `yaw_deg` |
| GLOBAL_POSITION_INT (33) | the `position` block, `rel_alt_m`, `climb_rate_mps` |
| VFR_HUD (74) | `airspeed_mps`, `throttle_pct` |
| BATTERY_STATUS (147) | `battery_temperature_c` (battery 0 only) |

MAVLink arrives at tens of messages per second. The gateway keeps the latest value of each metric and publishes one message per vehicle per interval, containing only what changed since the last one. Values the autopilot marks as "not measured" are left out, as is a 0,0 position from a receiver with no fix. Heartbeats from ground stations, gimbals and companion computers are ignored.

## Known limits

- **Timestamps are the gateway's clock**, taken at publish time. That suits fleet monitoring at 1 Hz. Bench work that correlates signals to the millisecond needs source timestamps and a PTP-synced clock, which this gateway does not provide.
- **QoS 0 only.** A reading published while the broker is down is lost. The gateway retries the connection every 2 seconds.
- **Seven messages are decoded.** Frames with any other message id are skipped by length, without checksum verification.
- **Signed MAVLink 2 frames are accepted but the signature is not verified.**
- **UDP only.** The parser handles byte streams, but serial and TCP inputs are not wired up.
- **Not benchmarked.** No throughput or latency figures have been measured yet.

## Adding a message

1. Add its id, `CRC_EXTRA` and base payload length to `definitions` in `internal/mavlink/messages.go`.
2. Write a decoder. Field offsets follow MAVLink wire order (largest types first), not XML order.
3. Add a frame to `tools/mavlink-sim/gen_golden.py`, run `make golden`, and assert the decoded values in `parser_test.go`.
4. Map it to metrics in `internal/telemetry/telemetry.go`. Metric names are `snake_case`.
