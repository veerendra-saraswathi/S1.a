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

Send MAVLink to UDP 14550 on the gateway host. For ArduPilot SITL, `make sitl` runs one
in Docker already pointed here ([tools/sitl](../../tools/sitl/README.md)). The gateway cannot
request streams, so the autopilot must be set to send them: on ArduPilot 4.7, `MAV1_*`
for SERIAL0 (`SR0_*` on older firmware).

## Options

| Flag | Environment | Default | Meaning |
|---|---|---|---|
| `-listen` | `GATEWAY_LISTEN` | `:14550` | UDP address for MAVLink |
| `-mqtt` | `S2_MQTT_URL` | `mqtt://localhost:1883` | Broker; `mqtts://` and `user:pass@` are supported |
| `-tenant` | `S2_TENANT` | S2's local dev tenant | Tenant UUID in the topic |
| `-object-type` | `GATEWAY_OBJECT_TYPE` | `CommercialDrone` | S2 object type |
| `-vehicles` | `GATEWAY_VEHICLES` | none | `1=DRN-0001,2=DRN-0002`; unmapped systems publish as `MAV-<id>` |
| `-rate-hz` | | `1` | Messages per vehicle per second, up to 50. Use 10 or more for bench work |
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
| SYSTEM_TIME (2) | none of its own; anchors the vehicle's clock (below) |

Every message also carries `clock_source`, and `clock_offset_ms` when it can be measured.

## Timestamps

`ts` is **source time**: when the newest timed sample in the message (ATTITUDE or
GLOBAL_POSITION_INT, from their `time_boot_ms`) was taken on the vehicle, in UTC.
`clock_source` says how the vehicle's boot clock was put on UTC:

| `clock_source` | How | Accuracy |
|---|---|---|
| `gnss` | SYSTEM_TIME pairs the autopilot's UTC, normally from its GNSS receiver, with `time_boot_ms`. Refreshed on each one; trusted for 5 s after the last | The autopilot's own UTC, to 1 ms |
| `estimated` | No usable SYSTEM_TIME (none sent, or UTC 0 before a fix). The boot epoch is the minimum of arrival minus `time_boot_ms` over a 10 s window, which strips link jitter and follows crystal drift | Spacing between samples exact; absolute time on the gateway's clock, early by the shortest link delay |
| `gateway` | The message holds no timed sample (heartbeats, battery) | The gateway's arrival time |

`clock_offset_ms` is the vehicle's UTC minus the gateway's arrival time, from the latest
SYSTEM_TIME: the clock skew between vehicle and ground, plus the one-way link delay.
A `time_boot_ms` that steps back by more than a second is a reboot; the vehicle's clock
starts over, and the 30-second log line counts reboots per system id.

The gateway never sends, so it cannot run MAVLink TIMESYNC.

MAVLink arrives at tens of messages per second. The gateway keeps the latest value of each metric and publishes one message per vehicle per interval, containing only what changed since the last one. Values the autopilot marks as "not measured" are left out, as is a 0,0 position from a receiver with no fix. Heartbeats from ground stations, gimbals and companion computers are ignored.

## Known limits

- **Source time is only as good as the autopilot's clock.** With `gnss` it is the autopilot's GNSS-derived UTC; with `estimated` it is the gateway's clock. Correlating signals from separate bench subsystems to better than a millisecond needs them on one PTP-disciplined clock, which is outside the gateway.
- **`ts` is per message, not per metric.** A message's metrics are the latest values since the last one and were sampled up to one interval apart. At 10 Hz that is 100 ms.
- **QoS 0 only.** A reading published while the broker is down is lost. The gateway retries the connection every 2 seconds.
- **Seven messages are decoded.** Frames with any other message id are skipped by length, without checksum verification.
- **Signed MAVLink 2 frames are accepted but the signature is not verified.**
- **UDP only.** The parser handles byte streams, but serial and TCP inputs are not wired up.
- **Benchmarked to 100 vehicles at 10 Hz** on a laptop: 9% of one core and 13 MB, every frame received ([benchmarks](../../docs/benchmarks.md)). The receive path costs 367 ns a frame. Higher counts and rates have not been run.

## Adding a message

1. Add its id, `CRC_EXTRA` and base payload length to `definitions` in `internal/mavlink/messages.go`.
2. Write a decoder. Field offsets follow MAVLink wire order (largest types first), not XML order.
3. Add a frame to `tools/mavlink-sim/gen_golden.py`, run `make golden`, and assert the decoded values in `parser_test.go`.
4. Map it to metrics in `internal/telemetry/telemetry.go`. Metric names are `snake_case`.
