# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt>=2.1", "confluent-kafka>=2.6", "psycopg[binary]>=3.2"]
# ///
"""Soak: simulated drones through mavlink-gateway into S2 for a long time, judged per minute.

bench.py times every message at every hop but holds them all until its step ends, which is
right for 20 s and wrong for an hour: 100 drones at 10 Hz is 3.6 million messages. This
runs one load for as long as asked and settles it a window at a time. Each minute's
messages are timed at the same hops as bench.py (it reuses its recorder, its Kafka
listener and its VM clock reading), counted, written out, and forgotten; only each
message's end-to-end latency is kept, as one float, for the overall percentiles.

    make build && uv run tools/bench/soak.py --drones 100 --minutes 60
    make soak DRONES=100 MINUTES=5                      # a smoke test

Every minute it also records what drifts in a long run and not in a short one: the
gateway's memory, each S2 ingestion container's CPU and memory, the sink group's Kafka
lag, the dead-letter topic, the Docker VM's clock, and this host's load and swap.

The verdict, in summary.json:

    lost         every message the broker delivered is stored, once the sinks have drained
                 what Kafka still holds (up to --drain s): late counts, lost does not
    dlq          nothing reached the dead-letter topic
    p95          end-to-end p95 under 500 ms overall, and in every minute
    drift        the last ten minutes' p95 within 1.5 times the first ten's, plus 50 ms
    lag          in the last minute the sinks were within one second of traffic, and stored
                 the rest within a minute of the run ending
    memory       no process's memory grew by more than half, first minute to last

A run that fails only on timing (p95, drift, lag) while the host was overloaded (a 1-minute
load over twice the Docker VM's CPUs in any minute, or swap growing by over 1 GB) is
INCONCLUSIVE: the host, not S2, was measured. Results go to bin/soak/<stamp>/.

Run it on a quiet machine, and keep it awake.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import uuid
from array import array
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from confluent_kafka import Consumer, KafkaException, TopicPartition

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench import (  # one way of timing a message, for the bench and the soak alike
    GATEWAY,
    ROOT,
    SIM,
    TENANT,
    Recorder,
    kafka_listener,
    parse_ts,
    pct,
    vm_clock_offset,
)

CONTAINERS = ["s2-mqtt", "s2-mqtt-bridge", "s2-kafka", "s2-timeseries-sink", "s2-timeseries-sink-2", "s2-timescaledb"]
TARGET_S = 0.5
GROUP = "s2-timeseries-sink"


def log(msg: str) -> None:
    print(f"soak: {msg}", file=sys.stderr, flush=True)


def stored_between(psql: list[str], prefix: str, start: float, end: float) -> dict[tuple[str, str], float]:
    """ingested_at per (key, ts) for messages whose ts falls in [start, end)."""
    a = datetime.fromtimestamp(start, UTC).isoformat()
    b = datetime.fromtimestamp(end, UTC).isoformat()
    query = (
        "copy (select object_pk, to_char(time at time zone 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'),"
        " extract(epoch from min(ingested_at)) from telemetry.reading"
        f" where time >= '{a}' and time < '{b}' and object_pk like '{prefix}%'"
        " group by 1, 2) to stdout with csv"
    )
    out = subprocess.run([*psql, "-Atc", query], check=True, capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        key, ts, ingested = line.split(",")
        rows[(key, ts)] = float(ingested)
    return rows


class Offsets:
    """The sink group's lag and the dead-letter topic's length, read from this host.

    Not with Kafka's own command-line tools: each starts a JVM inside the broker's container,
    and the soak would measure its own probes. This consumer never subscribes, so it does
    not join the sink group; it only reads the group's committed offsets.
    """

    def __init__(self, bootstrap: str, topic: str) -> None:
        self.topic = topic
        self.consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": GROUP, "enable.auto.commit": False})

    def partitions(self, topic: str) -> list[TopicPartition]:
        meta = self.consumer.list_topics(topic, timeout=10).topics[topic]
        return [TopicPartition(topic, p) for p in sorted(meta.partitions)]

    def sink_lag(self) -> int | None:
        try:
            parts = self.partitions(self.topic)
            committed = self.consumer.committed(parts, timeout=10)
            ends = [self.consumer.get_watermark_offsets(p, timeout=10)[1] for p in parts]
        # A probe that fails (a timeout gives None, a missing topic a KeyError) is a missing
        # number, not a failed soak.
        except (KafkaException, KeyError, TypeError):
            return None
        return sum(end - max(c.offset, 0) for c, end in zip(committed, ends))

    def dlq_end(self) -> int | None:
        try:
            return sum(self.consumer.get_watermark_offsets(p, timeout=10)[1]
                       for p in self.partitions("s2.telemetry.dlq"))
        except (KafkaException, KeyError, TypeError):
            return None

    def close(self) -> None:
        self.consumer.close()


def mib(text: str) -> float:
    number, unit = re.match(r"([\d.]+)\s*([KMG]i?B|B)", text).groups()
    return float(number) * {"B": 1 / 2**20, "KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1,
                            "GiB": 1024, "GB": 1024}[unit]


def running(names: list[str]) -> list[str]:
    """The ones that exist: docker stats refuses the lot if one name is missing."""
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], check=False, capture_output=True, text=True).stdout
    up = set(out.split())
    return [n for n in names if n in up]


def containers() -> dict[str, dict[str, float]]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}", *running(CONTAINERS)],
        check=False, capture_output=True, text=True,
    ).stdout
    stats = {}
    for line in out.splitlines():
        name, cpu, mem = line.split(" ", 2)
        stats[name.removeprefix("s2-")] = {"cpu_pct": float(cpu.rstrip("%")), "mem_mib": round(mib(mem.split(" / ")[0]), 1)}
    return stats


def swap_used_mib() -> float | None:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], check=False, capture_output=True, text=True).stdout
    match = re.search(r"used = ([\d.]+)M", out)
    return float(match[1]) if match else None


def gateway_usage(pid: int) -> tuple[float, float] | None:
    out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], check=False, capture_output=True, text=True).stdout.split()
    return (float(out[0]), round(float(out[1]) / 1024, 1)) if len(out) == 2 else None


def settle(recorder: Recorder, args, prefix: str, start: float, end: float, offset: float,
           totals: array, late: dict) -> dict:
    """Time and count the messages sampled in [start, end), then drop them from the recorder."""
    def in_window(item):
        return start <= parse_ts(item[0][1]) < end

    with recorder.lock:
        broker = dict(filter(in_window, recorder.broker.items()))
        kafka = {k: v for k, v in recorder.kafka.items() if start <= parse_ts(k[1]) < end}
        for k in broker:
            recorder.broker.pop(k, None)
        for k in kafka:
            recorder.kafka.pop(k, None)
    stored = stored_between(args.psql, prefix, start, end)

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
            total = stored[(key, ts)] - offset - source
            hops["total"].append(total)
            totals.append(total)
        else:
            late[(key, ts)] = source
    return {
        "at_broker": len(broker), "at_kafka": len(kafka), "stored": sum(1 for k in broker if k in stored),
        "latency_ms": {hop: {q: round(pct(v, q) * 1000, 1) for q in (50, 95, 99)}
                       | {"max": round(max(v) * 1000, 1) if v else None, "n": len(v)} for hop, v in hops.items()},
    }


def judge(windows: list[dict], totals: array, lost: int, late_found: int, drain_s: float, dlq_delta: int | None,
          drones: int, rate_hz: int, vm_cpus: int) -> dict:
    p95s = [w["latency_ms"]["total"][95] for w in windows if w["latency_ms"]["total"]["n"]]
    overall = {q: round(pct(list(totals), q) * 1000, 1) for q in (50, 95, 99)} if totals else {}
    first, last = p95s[:10], p95s[-10:]
    drift_ok = bool(first) and statistics.mean(last) <= 1.5 * statistics.mean(first) + 50
    final_lag = windows[-1].get("sink_lag") if windows else None
    mem = {}
    for name in windows[0]["containers"] if windows else []:
        a, b = windows[0]["containers"][name]["mem_mib"], windows[-1]["containers"].get(name, {}).get("mem_mib")
        mem[name] = (a, b)
    if windows and windows[0].get("gateway_rss_mib") and windows[-1].get("gateway_rss_mib"):
        mem["gateway"] = (windows[0]["gateway_rss_mib"], windows[-1]["gateway_rss_mib"])
    grown = {k: v for k, v in mem.items() if v[0] and v[1] and v[1] > 1.5 * v[0] and v[1] - v[0] > 20}

    checks = {
        "lost": (lost == 0, f"{lost} delivered by the broker and never stored; {late_found} stored late"),
        "dlq": (dlq_delta == 0, f"{dlq_delta} reached the dead-letter topic"),
        "p95": (bool(overall) and overall[95] < TARGET_S * 1000 and max(p95s) < TARGET_S * 1000,
                f"overall p95 {overall.get(95)} ms, worst minute {max(p95s) if p95s else None} ms"),
        "drift": (drift_ok, (f"first ten minutes' p95 {round(statistics.mean(first), 1) if first else None} ms,"
                             f" last ten {round(statistics.mean(last), 1) if last else None} ms")),
        # Kept up: at the last minute the sinks were within a second of traffic, and stored the
        # rest within a window of the run ending.
        "lag": (final_lag is not None and final_lag < drones * rate_hz and drain_s < 60,
                f"sink lag in the last minute {final_lag} messages, {drain_s} s to drain after the run"),
        "memory": (not grown, "flat" if not grown else f"grew: {grown}"),
    }
    failed = [name for name, (ok, _) in checks.items() if not ok]
    swaps = [w["swap_used_mib"] for w in windows if w.get("swap_used_mib") is not None]
    overloaded = any(w["load_1m"] > 2 * vm_cpus for w in windows) or (bool(swaps) and swaps[-1] - swaps[0] > 1024)
    verdict = "PASS" if not failed else "FAIL"
    if failed and set(failed) <= {"p95", "drift", "lag"} and overloaded:
        verdict = "INCONCLUSIVE"
    return {"verdict": verdict, "failed": failed, "host_overloaded": overloaded, "latency_ms": overall,
            "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--drones", type=int, default=100)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--rate-hz", type=int, default=10)
    parser.add_argument("--window", type=int, default=60, help="seconds per window")
    parser.add_argument("--settle", type=float, default=15, help="seconds a window is given to be stored")
    parser.add_argument("--drain", type=float, default=600,
                        help="seconds the sinks are given after the run to store a backlog")
    parser.add_argument("--port", type=int, default=14700)
    parser.add_argument("--mqtt", default=os.environ.get("S2_MQTT_URL", "mqtts://localhost:8883"))
    parser.add_argument("--mqtt-user", default="s2-monitor")
    parser.add_argument("--mqtt-password-file", default=os.environ.get("S2_MQTT_MONITOR_PASSWORD_FILE"))
    parser.add_argument("--mqtt-ca", default=os.environ.get("S2_MQTT_CA_FILE"))
    parser.add_argument("--kafka", default="localhost:29092")
    parser.add_argument("--topic", default="s2.telemetry.raw")
    parser.add_argument("--psql", default="docker exec s2-timescaledb psql -U postgres -d telemetry")
    parser.add_argument("--telemetry-url", default="postgresql://postgres:postgres_dev@localhost:5433/telemetry")
    args = parser.parse_args()
    args.psql = args.psql.split()
    if not GATEWAY.exists():
        sys.exit("bin/mavlink-gateway is missing: run `make build` first")
    if not os.environ.get("S2_MQTT_PASSWORD_FILE"):
        sys.exit("the gateway needs S2's broker login: run through `make soak`, which exports S2_MQTT_*")
    vm_cpus = int(subprocess.run(["docker", "info", "--format", "{{.NCPU}}"], check=False, capture_output=True,
                                 text=True).stdout or 0)

    begun = datetime.now(UTC)
    out = ROOT / "bin" / "soak" / begun.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    prefix = f"SOAK-{uuid.uuid4().hex[:4].upper()}-"
    vehicles = ",".join(f"{i}={prefix}{i:04d}" for i in range(1, args.drones + 1))
    recorder = Recorder(prefix)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"s1a-soak-{prefix}")
    client.on_message = recorder.on_mqtt
    broker = urlparse(args.mqtt)
    tls = broker.scheme in ("mqtts", "ssl")
    if args.mqtt_password_file:
        client.username_pw_set(args.mqtt_user, Path(args.mqtt_password_file).read_text().strip())
    if tls:
        client.tls_set(ca_certs=args.mqtt_ca or None)
    client.connect(broker.hostname or "localhost", broker.port or (8883 if tls else 1883))
    client.subscribe(f"s2/v1/{TENANT}/CommercialDrone/+/telemetry")
    client.loop_start()

    stop_kafka, kafka_ready = threading.Event(), threading.Event()
    kafka_thread = threading.Thread(target=kafka_listener,
                                    args=(args.kafka, args.topic, recorder, kafka_ready, stop_kafka), daemon=True)
    kafka_thread.start()
    if not kafka_ready.wait(30):
        sys.exit("the Kafka consumer was not assigned its partitions within 30 s")

    offsets = Offsets(args.kafka, args.topic)
    dlq_before = offsets.dlq_end()
    gateway_log = (out / "gateway.log").open("w")
    gateway = subprocess.Popen(
        [str(GATEWAY), "-listen", f"127.0.0.1:{args.port}", "-vehicles", vehicles, "-rate-hz", str(args.rate_hz),
         "-client-id", f"s1a-soak-gw-{prefix}"],
        stderr=gateway_log,
    )
    time.sleep(1)
    seconds = args.minutes * 60
    log(f"{args.drones} drones at {args.rate_hz} Hz for {args.minutes} min as {prefix}*, results in "
        f"{out.relative_to(ROOT)}")
    sim = subprocess.Popen(["uv", "run", "-q", str(SIM), "--drones", str(args.drones), "--seconds", str(seconds),
                            "--rate-hz", str(args.rate_hz), "--target", f"127.0.0.1:{args.port}"],
                           stdout=subprocess.DEVNULL, stderr=(out / "sim.log").open("w"))

    totals: array = array("d")
    late: dict[tuple[str, str], float] = {}
    windows: list[dict] = []
    start = time.time()
    offset_before, _ = vm_clock_offset(args.telemetry_url)
    try:
        with (out / "windows.jsonl").open("w") as lines:
            for index in range(-(-seconds // args.window)):
                w_start, w_end = start + index * args.window, min(start + (index + 1) * args.window, start + seconds)
                # Sample resources while the window's traffic flows, then let it be stored.
                time.sleep(max(0.0, w_end - time.time()))
                usage, stats, lag = gateway_usage(gateway.pid), containers(), offsets.sink_lag()
                time.sleep(max(0.0, w_end + args.settle - time.time()))
                offset_after, _ = vm_clock_offset(args.telemetry_url)
                window = {
                    "window": index + 1, "start": datetime.fromtimestamp(w_start, UTC).isoformat(timespec="seconds"),
                    "expected": round(args.drones * args.rate_hz * (w_end - w_start)),
                    **settle(recorder, args, prefix, w_start, w_end, (offset_before + offset_after) / 2, totals, late),
                    "gateway_cpu_pct": usage[0] if usage else None, "gateway_rss_mib": usage[1] if usage else None,
                    "containers": stats, "sink_lag": lag, "dlq": offsets.dlq_end(),
                    "vm_clock_offset_ms": round((offset_before + offset_after) / 2 * 1000, 1),
                    "load_1m": round(os.getloadavg()[0], 2), "swap_used_mib": swap_used_mib(),
                }
                offset_before = offset_after
                windows.append(window)
                lines.write(json.dumps(window) + "\n")
                lines.flush()
                total = window["latency_ms"]["total"]
                log(f"minute {index + 1}/{len(range(-(-seconds // args.window)))}: {window['stored']}/"
                    f"{window['at_broker']} stored, p95 {total[95]} ms, max {total['max']} ms, lag {lag}, "
                    f"load {window['load_1m']}")
                if sim.poll() not in (None, 0):
                    raise SystemExit(f"the simulator exited {sim.returncode}; see {out / 'sim.log'}")
    finally:
        sim.terminate()
        gateway.terminate()
        gateway.wait(timeout=10)
        stop_kafka.set()
        kafka_thread.join(timeout=10)
        client.loop_stop()
        client.disconnect()

    # Let the sinks finish what Kafka still holds before anything is called lost: a backlog
    # is late, not lost. At most --drain seconds, polled every 5.
    drain_started, lag = time.monotonic(), offsets.sink_lag()
    while lag and time.monotonic() - drain_started < args.drain:
        time.sleep(5)
        lag = offsets.sink_lag()
    drain_s = round(time.monotonic() - drain_started, 1)
    if lag:
        log(f"the sinks still had {lag} messages to store after {args.drain:g} s; those count as lost")
    elif drain_s > 1:
        log(f"the sinks took {drain_s} s after the run to store the backlog")

    # A message not stored within its window's settle time is late, not lost, if it is there now.
    found = 0
    if late:
        stored = stored_between(args.psql, prefix, min(late.values()), max(late.values()) + 0.001)
        found = sum(1 for k in late if k in stored)
    dlq_after = offsets.dlq_end()
    offsets.close()
    summary = {
        "began": begun.isoformat(timespec="seconds"), "prefix": prefix, "drones": args.drones,
        "minutes": args.minutes, "rate_hz": args.rate_hz,
        "expected": args.drones * args.rate_hz * seconds,
        "at_broker": sum(w["at_broker"] for w in windows), "stored": sum(w["stored"] for w in windows) + found,
        "late": found, "lost": len(late) - found, "drain_s": drain_s, "lag_after_drain": lag,
        "dlq_before": dlq_before, "dlq_after": dlq_after,
        **judge(windows, totals, len(late) - found, found, drain_s,
                None if None in (dlq_before, dlq_after) else dlq_after - dlq_before,
                args.drones, args.rate_hz, vm_cpus),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\n{summary['verdict']}: {args.drones} drones, {args.minutes} min, {summary['at_broker']} delivered, "
          f"{summary['stored']} stored, p50/p95/p99 {summary['latency_ms']} ms")
    for name, check in summary["checks"].items():
        print(f"  {'ok  ' if check['ok'] else 'FAIL'} {name:7} {check['detail']}")
    log(f"summary: {(out / 'summary.json').relative_to(ROOT)}")
    if summary["verdict"] == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()
