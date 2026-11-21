# Benchmarks

What has been measured about S1.a's telemetry path, how, and under which conditions. The
Samanvaya reference sets the targets: 50 assets per node at 10 Hz, and telemetry from
sample to operator screen in under 500 ms at p95 on SITL (§6). The screen is not
measured here; the last hop measured is the row landing in TimescaleDB.

## The harness

`tools/bench/bench.py` runs the MAVLink simulator with N drones through `mavlink-gateway`
into a running S2 stack. It times every message at each hop:

| Hop | From | To | Clock |
|---|---|---|---|
| Source → broker | `ts` in the payload: the sample time on the vehicle | the harness's MQTT subscriber receives it | host |
| Broker → bridge | the harness receives it | S2's mqtt-bridge stamps `received_at` | Docker VM |
| Bridge → Kafka | `received_at` | the harness's Kafka consumer receives the envelope | host |
| Kafka → stored | the harness's consumer | the row's `ingested_at` in TimescaleDB | Docker VM |

Before loading anything it calibrates the Docker VM's clock against the host's: the broker
hands each probe message to the harness and the bridge at the same moment, so the median
difference is the offset, and it is subtracted from every VM-side time. It samples the
gateway's CPU and memory and the CPU of S2's ingestion containers during load, and after
each step waits for S2 to drain, so "stored" counts every message that ever arrived.

```bash
make build
uv run tools/bench/bench.py --steps 3,10,50,100 --seconds 20 --json results.json
```

Bridge → Kafka includes up to 50 ms of the harness's own Kafka polling. Run it on a quiet
machine: anything else on Docker's CPUs, or a host that swaps, shows up as latency.

## Results

### Gateway receive paths (Go benchmarks)

`go test -bench . ./mavlink-gateway/internal/telemetry ./can-gateway/internal/bus`, on an
Intel Core i7-9750H, 2.6 GHz, one core:

| Path | Cost | Throughput |
|---|---|---|
| MAVLink: bytes → frames → aggregator | 367 ns per frame | about 2.7 million frames a second, against about 34 a second per vehicle |
| CAN: candump line → transfer → CRC → decode → aggregate | 1.24 µs per line | about 800,000 lines a second |

Neither gateway is near being the bottleneck.

### End to end, before the change (2026-11-20, quiet host)

The kind clusters that normally share Docker's 5 CPUs were stopped; host load about 12 on
12 cores; S2 as shipped, the sink waiting up to 1.0 s for a batch. 20 s per step at 10 Hz.

| Drones | At broker | Stored | Gateway CPU mean / max, RSS | Source → broker p50 / p95 | Bridge → Kafka p50 / p95 | Kafka → stored p50 / p95 | **Total p50 / p95 / p99** |
|---|---|---|---|---|---|---|---|
| 3 | 600 | 600 | 0.5% / 1.5%, 9.9 MB | 22 / 24 ms | 45 / 72 ms | 497 / 962 ms | **558 / 1,018 / 1,054 ms** |
| 10 | 2,000 | 2,000 | 1.1% / 2.7%, 12.1 MB | 12 / 15 ms | 47 / 72 ms | 547 / 1,025 ms | **584 / 1,091 / 1,133 ms** |
| 50 | 10,000 | 10,000 | 4.1% / 8.9%, 12.7 MB | 91 / 95 ms | 42 / 71 ms | 453 / 895 ms | **617 / 1,027 / 1,316 ms** |
| 100 | 19,847 | 19,847 | 8.6% / 20.7%, 13.3 MB | 41 / 50 ms | 47 / 72 ms | 2,856 / 3,431 ms | **2,949 / 3,523 / 4,693 ms** |

Broker → bridge was 0-3 ms at p50 throughout. Nothing was lost at any step; S2 drained
within 3 s of each one. The shortfall at the broker at 100 drones is the Python simulator
falling behind its 10 Hz loop, not loss: the gateway received every frame it was sent.

**What it shows.** The gateway, broker, bridge and Kafka together take under 150 ms. The
sink takes the rest. Its Kafka → stored time is spread evenly from 0 to 1 s (p50 ≈ 0.5 s,
p95 ≈ 0.95 s), which is S2's sink waiting up to a second to fill a batch of 500 that
light traffic never fills: `consumer.consume(num_messages=500, timeout=1.0)`. That wait
alone puts p95 over the 500 ms target at every load. At 100 drones, twice the target
load, TimescaleDB reached 118% CPU and the sink 74%, and the sink fell behind.

### The change

S2's sink now waits at most `S2_SINK_BATCH_WAIT_S` (default 0.1 s) for a batch, and still
takes up to `S2_SINK_BATCH_SIZE` (500) at once, so batches fill under load as before.

Measured right after, on the same host (load about 16):

| Drones | Kafka → stored p50 / p95 | **Total p50 / p95 / p99** |
|---|---|---|
| 3 | 47 / 104 ms | **188 / 258 / 333 ms** |
| 10 | 100 / 391 ms | **257 / 708 / 1,208 ms** |

At light load the change does what it should: total p95 fell from 1,018 to 258 ms, inside
the 500 ms target.

The runs that followed at 50 and 100 drones gave 14-31 s, but by then the host's load had
risen from 16 to 70 and it was out of memory (15.6 of 17 GB of swap in use). Those numbers
measured the swapping, not S2, and are not reported as results.

### A second change: one statement per batch (2026-11-21)

S2's sink wrote each row with its own `INSERT` (`executemany`), so a batch of 500
envelopes cost TimescaleDB about 9,000 parses, plans and round trips. It now sends each
table's rows as one array per column and inserts them with one `INSERT … SELECT FROM
unnest(…)`. `ON CONFLICT DO NOTHING` still makes a replayed batch, or a row repeated inside
one, a no-op.

An A/B test on a throwaway database alternated the two ways of writing, one fresh batch
each per round, 30 rounds. Each batch was 500 envelopes from 50 drones, 17 metrics and a
position each: 8,500 readings and 500 positions. Kind clusters stopped, host load about 5.

| Write | Median | p10 | p90 | Rows a second at the median |
|---|---|---|---|---|
| executemany, one `INSERT` per row | 864 ms | 769 ms | 1,282 ms | 10,417 |
| `unnest`, one `INSERT` per table | 398 ms | 308 ms | 530 ms | **22,618** |

2.2 times faster. 100 drones at 10 Hz is about 18,000 rows a second, more than the old way
could write.

### End to end, after both changes (2026-11-21, quiet host)

The kind clusters were stopped; host load 4-5 on 12 cores. The sink waits up to 0.1 s for a
batch of up to 500 and writes it with `unnest`. 20 s per step at 10 Hz.

| Drones | At broker | Stored | Gateway CPU mean / max, RSS | Source → broker p50 / p95 | Bridge → Kafka p50 / p95 | Kafka → stored p50 / p95 | **Total p50 / p95 / p99** | Peak CPU: bridge / sink / TimescaleDB |
|---|---|---|---|---|---|---|---|---|
| 3 | 597 | 597 | 0.6% / 1.2%, 8.7 MB | 26 / 32 ms | 63 / 90 ms | 14 / 81 ms | **89 / 146 / 271 ms** | 8% / 10% / 10% |
| 10 | 2,000 | 2,000 | 1.5% / 2.4%, 12.2 MB | 56 / 59 ms | 63 / 88 ms | 19 / 81 ms | **125 / 175 / 205 ms** | 3% / 7% / 13% |
| 50 | 10,000 | 10,000 | 7.8% / 13.4%, 13.5 MB | 75 / 79 ms | 62 / 86 ms | 41 / 117 ms | **167 / 246 / 390 ms** | 13% / 24% / 38% |
| 100 | 19,900 | 19,900 | 12.4% / 17.3%, 12.7 MB | 48 / 55 ms | 58 / 85 ms | 80 / 198 ms | **180 / 293 / 347 ms** | 23% / 47% / 45% |

**What it shows.** p95 is inside the 500 ms target at every step, 100 drones (twice the
target load) included, where it was 3.5 s before. The sink no longer falls behind:
TimescaleDB peaked at 45% CPU at 100 drones, where it reached 118% before. Every message
that reached the broker was stored. The shortfall at the broker, 3 at 3 drones and 100 at
100, is the Python simulator missing ticks, as before.

Broker → bridge came out at -7 to -16 ms, which is impossible. The clock calibration
(+13.2 ms on this run) is off by about that much, so VM-side times, Total among them, may
be up to about 15 ms too low. That doesn't change any conclusion here. Swap use also grew
during the run, from 5.6 to 9.6 GB of 10 GB, but the load stayed at 4-5, so the host was
not thrashing the way it was on 2026-11-20.

### Not established

The operator screen; the CAN gateway end to end under load; a degraded link; anything
past 100 drones; sustained load longer than 20 s; and these numbers on the dedicated test
hardware the reference calls for (§4.10) rather than a 16 GB laptop.
