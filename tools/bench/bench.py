# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt>=2.1", "confluent-kafka>=2.6"]
# ///
"""End-to-end benchmark: simulated drones -> mavlink-gateway -> S2 -> TimescaleDB.

For each load step it runs the MAVLink simulator with N drones through the gateway into a
running S2 stack, and times every message at each hop by its own clock:

    source   ts in the payload: when the newest sample was taken on the vehicle
    broker   this harness's MQTT subscriber receives it          (gateway + broker)
    bridge   envelope.received_at, set by S2's mqtt-bridge         (broker -> bridge)
    kafka    this harness's Kafka consumer receives the envelope   (bridge -> Kafka)
    stored   ingested_at of its row in TimescaleDB                 (Kafka -> sink -> DB)

It also samples the gateway's CPU and memory, and counts messages lost at each hop.

    make build && uv run tools/bench/bench.py                        # steps 3, 10, 50 drones
    uv run tools/bench/bench.py --steps 10,50,100 --seconds 30 --json results.json

The S2 stack must be up (`make up` in S2). `bridge` and `stored` are read on the clock of
Docker's VM, the rest on this host's, and the two can disagree by tens of milliseconds.
Before loading anything, the harness calibrates: it publishes probe messages at a
trickle, and since the broker hands each one to this harness and to the bridge at the
same moment, the median of (bridge receive - harness receive) is the VM clock's offset.
It is reported as `vm_clock_offset_ms` and subtracted from every VM-side time.

During each step it also samples the CPU of S2's ingestion containers, which shows
which stage saturates first.

Run it on a quiet machine. Anything else using Docker's CPUs shows up as latency.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from confluent_kafka import Consumer, TopicPartition

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "bin" / "mavlink-gateway"
SIM = ROOT / "tools" / "mavlink-sim" / "sim.py"
TENANT = "00000000-0000-0000-0000-000000000001"


def parse_ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q / 100 * len(ordered)))]


class Recorder:
    """Arrival times per (key, ts) at the broker and at Kafka."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.lock = threading.Lock()
        self.broker: dict[tuple[str, str], float] = {}
        self.kafka: dict[tuple[str, str], tuple[float, float]] = {}  # -> (bridge received_at, arrival)

    def on_mqtt(self, _client, _userdata, message) -> None:
        now = time.time()
        key = message.topic.split("/")[4]
        if not key.startswith(self.prefix):
            return
        ts = json.loads(message.payload)["ts"]
        with self.lock:
            self.broker[(key, ts)] = now

    def on_kafka(self, value: bytes) -> None:
        now = time.time()
        envelope = json.loads(value)
        key = envelope["object_pk"]
        if not key.startswith(self.prefix):
            return
        ts = envelope["message"]["ts"]
        # The bridge re-serialises ts; normalise to the gateway's millisecond form.
        ts = datetime.fromisoformat(ts).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with self.lock:
            self.kafka[(key, ts)] = (parse_ts(envelope["received_at"]), now)


def kafka_listener(bootstrap: str, topic: str, recorder: Recorder, ready: threading.Event, stop: threading.Event):
    consumer = Consumer({
        "bootstrap.servers": bootstrap, "group.id": f"s1a-bench-{uuid.uuid4().hex[:8]}",
        "enable.auto.commit": False, "auto.offset.reset": "latest",
    })

    def assigned(c, partitions):
        # Start at the end now, not at whatever "latest" means after a rebalance.
        for p in partitions:
            p.offset = c.get_watermark_offsets(TopicPartition(topic, p.partition), timeout=5)[1]
        c.assign(partitions)
        ready.set()

    consumer.subscribe([topic], on_assign=assigned)
    try:
        while not stop.is_set():
            for message in consumer.consume(num_messages=1000, timeout=0.05):
                if message.error() is None:
                    recorder.on_kafka(message.value())
    finally:
        consumer.close()


def stored_rows(psql: list[str], prefix: str, since: str) -> dict[tuple[str, str], float]:
    """ingested_at per (key, ts), from the battery_voltage-or-roll reading every message carries."""
    query = (
        "copy (select object_pk, to_char(time at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'),"
        " extract(epoch from min(ingested_at)) from telemetry.reading"
        # The time bound lets TimescaleDB read only the recent chunks.
        f" where time >= timestamptz '{since}' - interval '1 minute'"
        f" and object_pk like '{prefix}%' and ingested_at >= '{since}'"
        " group by 1, 2) to stdout with csv"
    )
    out = subprocess.run([*psql, "-Atc", query], check=True, capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        key, ts, ingested = line.split(",")
        rows[(key, ts)] = float(ingested)
    return rows


CONTAINERS = ["s2-mqtt", "s2-mqtt-bridge", "s2-kafka", "s2-timeseries-sink", "s2-timescaledb"]


def sample_containers(samples: dict[str, list[float]], stop: threading.Event) -> None:
    while not stop.is_set():
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", *CONTAINERS],
            check=False, capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            name, _, cpu = line.partition(" ")
            if name in samples and cpu.endswith("%"):
                samples[name].append(float(cpu[:-1]))


def calibrate(args) -> float:
    """The Docker VM clock minus this host's, in seconds (see the module docstring)."""
    prefix = f"BENCH-CAL-{uuid.uuid4().hex[:6].upper()}"
    recorder = Recorder(prefix)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"s1a-bench-{prefix}")
    client.on_message = recorder.on_mqtt
    client.connect(args.mqtt_host, args.mqtt_port)
    topic = f"s2/v1/{TENANT}/CommercialDrone/{prefix}/telemetry"
    client.subscribe(topic)
    client.loop_start()
    stop, ready = threading.Event(), threading.Event()
    thread = threading.Thread(target=kafka_listener, args=(args.kafka, args.topic, recorder, ready, stop), daemon=True)
    thread.start()
    if not ready.wait(20):
        sys.exit("the Kafka consumer was not assigned its partitions within 20 s")
    time.sleep(0.5)
    for i in range(40):
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        client.publish(topic, json.dumps({"ts": ts, "metrics": {"bench_probe": i}, "seq": i}))
        time.sleep(0.05)
    time.sleep(3)
    stop.set()
    thread.join(timeout=10)
    client.loop_stop()
    client.disconnect()
    with recorder.lock:
        deltas = [recorder.kafka[k][0] - at for k, at in recorder.broker.items() if k in recorder.kafka]
    if len(deltas) < 20:
        sys.exit(f"calibration saw only {len(deltas)} of 40 probes through the bridge; is S2's ingestion running?")
    return statistics.median(deltas)


def sample_process(pid: int, samples: list[tuple[float, float]], stop: threading.Event) -> None:
    while not stop.wait(1.0):
        out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], check=False, capture_output=True, text=True).stdout.split()
        if len(out) == 2:
            samples.append((float(out[0]), float(out[1]) / 1024))


def run_step(args, drones: int, port: int, offset: float) -> dict:
    prefix = f"BENCH-{uuid.uuid4().hex[:6].upper()}-"
    vehicles = ",".join(f"{i}={prefix}{i:04d}" for i in range(1, drones + 1))
    recorder = Recorder(prefix)
    since = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"s1a-bench-{prefix}")
    client.on_message = recorder.on_mqtt
    client.connect(args.mqtt_host, args.mqtt_port)
    client.subscribe(f"s2/v1/{TENANT}/CommercialDrone/+/telemetry")
    client.loop_start()

    stop_kafka, kafka_ready = threading.Event(), threading.Event()
    kafka_thread = threading.Thread(
        target=kafka_listener, args=(args.kafka, args.topic, recorder, kafka_ready, stop_kafka), daemon=True
    )
    kafka_thread.start()
    if not kafka_ready.wait(20):
        sys.exit("the Kafka consumer was not assigned its partitions within 20 s")

    gateway = subprocess.Popen(
        [str(GATEWAY), "-listen", f"127.0.0.1:{port}", "-vehicles", vehicles, "-rate-hz", str(args.rate_hz),
         "-client-id", f"s1a-bench-gw-{prefix}"],
        stderr=subprocess.PIPE, text=True,
    )
    samples: list[tuple[float, float]] = []
    containers: dict[str, list[float]] = {name: [] for name in CONTAINERS}
    stop_sampling = threading.Event()
    threading.Thread(target=sample_process, args=(gateway.pid, samples, stop_sampling), daemon=True).start()
    threading.Thread(target=sample_containers, args=(containers, stop_sampling), daemon=True).start()
    time.sleep(1)

    started = time.monotonic()
    subprocess.run(
        ["uv", "run", "-q", str(SIM), "--drones", str(drones), "--seconds", str(args.seconds),
         "--target", f"127.0.0.1:{port}"],
        check=True, stdout=subprocess.DEVNULL,
    )
    sim_wall = time.monotonic() - started
    stop_sampling.set()  # resources are measured under load only, not while the harness polls the database
    time.sleep(args.settle)
    # Wait for S2 to drain: until the stored count holds still for 3 s, at most 3 minutes.
    drain_started, last, steady = time.monotonic(), -1, 0
    while time.monotonic() - drain_started < 180 and steady < 3:
        count = len(stored_rows(args.psql, prefix, since))
        steady = steady + 1 if count == last else 0
        last = count
        time.sleep(1)
    drain_s = time.monotonic() - drain_started - steady

    gateway.terminate()
    _, gateway_log = gateway.communicate(timeout=10)
    stop_kafka.set()
    kafka_thread.join(timeout=10)
    client.loop_stop()
    client.disconnect()

    stored = stored_rows(args.psql, prefix, since)
    with recorder.lock:
        broker, kafka = dict(recorder.broker), dict(recorder.kafka)

    hops = {"source_to_broker": [], "broker_to_bridge": [], "bridge_to_kafka": [], "kafka_to_stored": [], "total": []}
    for (key, ts), at_broker in broker.items():
        source = parse_ts(ts)
        hops["source_to_broker"].append(at_broker - source)
        if (key, ts) in kafka:
            received_at, at_kafka = kafka[(key, ts)]
            hops["broker_to_bridge"].append(received_at - offset - at_broker)
            hops["bridge_to_kafka"].append(at_kafka - (received_at - offset))
            if (key, ts) in stored:
                hops["kafka_to_stored"].append(stored[(key, ts)] - offset - at_kafka)
        if (key, ts) in stored:
            hops["total"].append(stored[(key, ts)] - offset - source)

    cpu = [c for c, _ in samples]
    stopping = next((line for line in gateway_log.splitlines() if "stopping:" in line), "")
    return {
        "drones": drones,
        "rate_hz": args.rate_hz,
        "seconds": args.seconds,
        "sim_wall_s": round(sim_wall, 2),
        "drain_after_settle_s": round(max(0.0, drain_s), 1),
        "expected": drones * args.rate_hz * args.seconds,
        "at_broker": len(broker),
        "at_kafka": len(kafka),
        "stored": len(stored),
        "gateway_cpu_pct_mean": round(statistics.mean(cpu), 1) if cpu else None,
        "gateway_cpu_pct_max": round(max(cpu), 1) if cpu else None,
        "gateway_rss_mb_max": round(max(m for _, m in samples), 1) if samples else None,
        "gateway_log": stopping.split("stopping: ", 1)[-1],
        "vm_clock_offset_ms": round(offset * 1000, 1),
        "s2_cpu_pct_max": {name.removeprefix("s2-"): max(v) if v else None for name, v in containers.items()},
        "latency_ms": {
            hop: {q: round(pct(v, q) * 1000, 1) for q in (50, 95, 99)} | {"n": len(v)} for hop, v in hops.items()
        },
    }


def table(results: list[dict]) -> str:
    lines = [
        "| Drones | Expected | At broker | Stored | Gateway CPU mean / max, RSS | Source→broker p50 / p95 | Broker→bridge p50 / p95 | Bridge→Kafka p50 / p95 | Kafka→stored p50 / p95 | **Total p50 / p95 / p99** | Peak CPU: bridge / sink / TimescaleDB |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lat, cpu = r["latency_ms"], r["s2_cpu_pct_max"]
        lines.append(
            f"| {r['drones']} | {r['expected']} | {r['at_broker']} | {r['stored']} "
            f"| {r['gateway_cpu_pct_mean']}% / {r['gateway_cpu_pct_max']}%, {r['gateway_rss_mb_max']} MB "
            f"| {lat['source_to_broker'][50]} / {lat['source_to_broker'][95]} ms "
            f"| {lat['broker_to_bridge'][50]} / {lat['broker_to_bridge'][95]} ms "
            f"| {lat['bridge_to_kafka'][50]} / {lat['bridge_to_kafka'][95]} ms "
            f"| {lat['kafka_to_stored'][50]} / {lat['kafka_to_stored'][95]} ms "
            f"| **{lat['total'][50]} / {lat['total'][95]} / {lat['total'][99]} ms** "
            f"| {cpu['mqtt-bridge']}% / {cpu['timeseries-sink']}% / {cpu['timescaledb']}% |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", default="3,10,50", help="drone counts, comma-separated")
    parser.add_argument("--seconds", type=int, default=20, help="length of each step")
    parser.add_argument("--rate-hz", type=int, default=10, help="gateway publish rate per drone")
    parser.add_argument("--settle", type=float, default=5, help="seconds to wait for the pipeline to drain")
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--kafka", default="localhost:29092")
    parser.add_argument("--topic", default="s2.telemetry.raw")
    parser.add_argument("--psql", default="docker exec s2-timescaledb psql -U postgres -d telemetry",
                        help="command that runs psql against S2's telemetry database")
    parser.add_argument("--json", help="also write the results here")
    args = parser.parse_args()
    args.psql = args.psql.split()
    if not GATEWAY.exists():
        sys.exit("bin/mavlink-gateway is missing: run `make build` first")

    offset = calibrate(args)
    print(f"Docker VM clock is {offset * 1000:+.1f} ms from this host's", file=sys.stderr, flush=True)
    results = []
    for i, drones in enumerate(int(s) for s in args.steps.split(",")):
        print(f"step {i + 1}: {drones} drones at {args.rate_hz} Hz for {args.seconds} s ...", file=sys.stderr, flush=True)
        result = run_step(args, drones, 14600 + i, offset)
        print(json.dumps(result), file=sys.stderr, flush=True)
        results.append(result)

    print(table(results))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"when": datetime.now(UTC).isoformat(timespec="seconds"), "machine": os.uname().machine,
             "results": results}, indent=2))


if __name__ == "__main__":
    main()
