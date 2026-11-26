# ArduCopter SITL

ArduPilot's own flight code, flying a simulated quadcopter in Docker and streaming
MAVLink to `mavlink-gateway`. Unlike `tools/mavlink-sim`, which writes plausible frames,
the frames here come from a real autopilot: its EKF, pre-arm checks, flight modes,
stream scheduler and GPS-derived clock.

```
arducopter (Docker) --SERIAL0, UDP 14550--> mavlink-gateway --MQTT--> S2
        ^
        +--SERIAL1, TCP 5762-- fly.py (arm, take off, square, RTL)
        +--SERIAL2, TCP 5763-- mavlink-gateway -input tcp:127.0.0.1:5763 (optional)
```

The gateway still only listens. Commands go to SITL on a separate port, from a script
that refuses to fly anything without a `SIM_SPEEDUP` parameter, which only SITL builds have.

## Run

Needs Docker and uv. With S2 up (`make up` in the S2 repo):

```bash
make sitl         # build the image on first use, start SITL
make sitl-gw      # in a second terminal: the gateway at 10 Hz, publishing as DRN-0001
make sitl-fly     # in a third: about 2.5 minutes from pre-arm to disarm
make sitl-stop
```

`make sitl-gw SITL_VEHICLE=DRN-0002` publishes under another S2 key. To exercise the
gateway's stream input instead of UDP, connect it to SERIAL2, which streams the same
telemetry: `./bin/mavlink-gateway -input tcp:127.0.0.1:5763 -vehicles 1=DRN-0001 -rate-hz 10`. The sortie's
altitude and square are `fly.py --alt` and `--side`.

## The image

`ArduCopter stable` for Linux x86_64 from `firmware.ardupilot.org`, statically linked,
and ArduPilot's default SITL copter parameters from the commit it was built from. Both
are pinned by SHA-256 in the `Dockerfile`; a changed file fails the build. On an Apple
Silicon Mac the image runs under Rosetta; that has not been tried.

The container wipes its EEPROM on every start, so each run begins from the same
parameters. `entrypoint.sh` takes `GATEWAY_HOST`, `GATEWAY_PORT`, `SYSID`, `SITL_HOME`
and `SPEEDUP`; keep `SPEEDUP=1`, since source timestamps are only meaningful in real time.

## Things that are not obvious

- **The gateway cannot request streams**, so `streams.parm` sets them on the autopilot.
  ArduPilot 4.7 renamed `SRn_*` to `MAVn_*`, numbered over MAVLink ports in order:
  `MAV1` is SERIAL0, `MAV3` SERIAL2. With the old names the parameters are silently
  ignored and only heartbeats arrive.
- **SITL's `udpclient:` takes an IPv4 address.** Given a hostname it logs the connection
  and sends nothing, so the entrypoint resolves `host.docker.internal` first.
- **ArduCopter may accept arming before its EKF has a position**, then refuse the GUIDED
  takeoff and disarm itself 10 s later. `fly.py` waits for a 3D fix and the EKF's
  absolute horizontal position, then switches to GUIDED and arms.
- **SITL keeps real time only on a quiet host.** On a loaded Mac it fell to 98% of real
  time: the clock offset sank half a second in 30 s and the rate dropped to 9.3 Hz.
  `tools/pipeline` measures this from the offset's drift and calls such a run
  INCONCLUSIVE rather than FAIL.
- **In GUIDED, a position setpoint cancels a takeoff.** ArduCopter's position controller
  will not lift a landed vehicle, so `fly.py` watches the climb and only then steers.
- **Docker accepts TCP connections on a published port while SITL is down**, and closes
  them at once. The gateway waits 2 s between attempts and counts a link as up only when
  bytes arrive, so a restart costs a few log lines, not a reconnect loop.
- **"Waiting for internal clock bits to be set"** is printed on every start and is not a stall.

## What a sortie produced

One flight on 2026-11-26, S2 `phase2-action-executor` at `bb47c45`, Intel Mac:

| Measure | Value |
|---|---|
| Duration, first message to disarm | 175 s |
| Messages stored | 1,745 at 10 Hz, 19 metrics, 18,573 readings; 1,816 positions |
| Gaps over 150 ms | 2, both in the first second, before SITL's streams started |
| `clock_source` | `gnss` 1,671, `estimated` 75 (before the first GPS fix), `gateway` 2 |
| Highest altitude, airspeed, throttle | 30.08 m, 10.1 m/s, 51% |
| `clock_offset_ms` in flight | −461.5 mean, 5.1 standard deviation |
| Vehicle sample to TimescaleDB row, after the offset | p50 148 ms, p95 196 ms, max 233 ms |

Two findings:

- **SITL's GNSS clock runs about 460 ms behind the host.** Stored `ts` is the vehicle's
  time, so ingest minus `ts` reads 607 ms at p50 until the offset is added back. This is
  the simulator's GPS model, not the pipeline; a real receiver should be within tens of
  milliseconds. It shows why `clock_offset_ms` is stored with every message.
- **The first `SYS_STATUS` after boot reports 0 V.** MAVLink reserves `UINT16_MAX` for
  "not measured", so the gateway passes 0 on as a reading. One row per boot; a consumer
  alarming on low voltage should ignore it until the vehicle is armed or reads above 0.
