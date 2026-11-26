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

Before and after every step it reads the Docker VM's clock against the host's the way NTP
does: about 200 `clock_timestamp()` queries to TimescaleDB, which runs on the VM's clock,
keeping the one with the shortest round trip (under 1 ms). Each step subtracts the mean
of its two readings from every VM-side time. A step whose readings differ by more than
2 ms is flagged, because Docker Desktop resyncs the VM clock in steps. It samples the
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

Broker → bridge came out at -7 to -16 ms, which is impossible. The harness then measured
the VM clock once, at the start (+13.2 ms), and the clock moved after that: Docker had
restarted half an hour earlier, and 50 minutes later the same clock read -2 ms by two
independent methods. VM-side times, Total among them, are therefore up to about 15 ms
too low on this run. That doesn't change any conclusion here, and the harness now
re-reads the clock around every step. Swap use also grew
during the run, from 5.6 to 9.6 GB of 10 GB, but the load stayed at 4-5, so the host was
not thrashing the way it was on 2026-11-20.

### One hour at 100 drones (2026-11-27, `make soak`)

`tools/bench/soak.py`: 100 simulated drones at 10 Hz for 60 minutes through the gateway over
TLS into S2, settled and judged a minute at a time. S2 as of `ccd07a8`: the strict telemetry
contract v1, `s2.telemetry.raw` on 4 partitions, two sinks in one group, both databases
under an init. Chrome was quit; Docker Desktop's Kubernetes was left running (about 20% of a
CPU). Swap was in use throughout, 1.4 to 9.8 GB of the host's swap file.

| Minutes | Delivered by the broker | Stored | p95 per minute | Host load (1 min) |
|---|---|---|---|---|
| 1-60 | 3,511,364 | 3,511,364 | 175 ms to 62.9 s | 2.8 to 71.9 |
| 30-60 | 1,856,704 | 1,856,704, each in its own minute | **175 to 270 ms** (p99 at most 627 ms) | 2.8 to 10.3 |

**Verdict: INCONCLUSIVE.** Nothing was lost and nothing reached the dead-letter topic: every
message the broker delivered was stored, 151,708 of them later than their minute. Memory was
flat (the gateway 13.0 to 13.4 MB over the last half hour), and the sinks had drained by the
end. The timing failed: 18 minutes, all in the first half hour (2-4 and 11-29), had p95
between 1.7 and 62.9 s. In each of those minutes the host's load stood between 5.5 and 72
and swap swung by gigabytes, and every hop slowed at once, gateway → broker included, which
runs on the host and not in S2: the host stalled, and S2 caught up after each stall. The
harness calls that INCONCLUSIVE, not FAIL.

**What it shows.** Thirty consecutive minutes at 100 drones, twice the reference's load per
node, met the 500 ms target in every minute, with TLS on and nothing lost. A long run also
proved what a 20 s step cannot: a backlog of 60,000 messages, built in a stall, was stored
within minutes with nothing lost or duplicated, and TimescaleDB, which crash-restarted twice
in the 5-minute smoke test before the init fix, did not restart once at load 72. It does not
show the hour inside the target: that needs a host that does not stall.

A 5-minute smoke test the same night, at load 28 to 105 with Chrome running, fell 116,000
messages behind and drained afterwards at about 300 messages a second, far below the 22,600
rows a second of the quiet A/B. One sink against two on a quiet host is still to be
compared.

### Not established

The operator screen; the CAN gateway end to end under load; a degraded link; anything
past 100 drones; an hour inside the target, since the soak's host stalled in its first half hour; and these numbers on the dedicated test
hardware the reference calls for (§4.10) rather than a 16 GB laptop.
