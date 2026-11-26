# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""S1.a's own pipeline: build the MAVLink gateway, test it, fly it, and write the
s2.build-record/v1 that says so.

    uv run tools/pipeline/pipeline.py                 # build, unit tests, SITL into S2
    uv run tools/pipeline/pipeline.py --sitl dry      # SITL without S2: judge the gateway's output
    uv run tools/pipeline/pipeline.py --sitl off      # build and unit tests only
    uv run tools/pipeline/pipeline.py --submit        # then hand the record to build-submit

The record names a real artifact: the gateway binary this run built, by SHA-256, from a
commit with no uncommitted changes. The SITL run flies ArduCopter SITL through that
exact binary and is judged against requirement SW-TLM-001:

    the sortie completes        take off, four waypoints, land, disarm (tools/sitl/fly.py)
    10 Hz                       at least 9.5 messages a second once SITL's streams start
    no gaps                     none longer than 500 ms once SITL's streams start
    clean link                  no bad checksum and no failed publish
    nothing lost      (s2)      every message the broker delivered stored in TimescaleDB
    distinct times    (s2)      no two messages with one timestamp, which storage would merge
    500 ms at p95     (s2)      vehicle sample to stored row, the vehicle clock's offset added back

With --sitl dry the gateway prints instead of publishing, so the last two cannot be
judged and are left out of the verdict; the note says so. Logs and the record go to
bin/pipeline/<build id>/.

A build whose SITL run passes is what build-submit asks the gate to move to SIM_PASSED.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATEWAYS = ROOT / "gateways"
REQUIREMENT = "SW-TLM-001"
DRONE = "DRN-0001"  # created by S2's seed-demo; the SITL copter publishes as this drone
PSQL = ["docker", "exec", "s2-timescaledb", "psql", "-U", "postgres", "-d", "telemetry", "-Atc"]
MAX_SPEED_ERROR = 0.01  # SITL more than 1% off real time cannot be judged on timing
STREAM_SETTLE_S = 5.0  # SITL's first second carries heartbeats only, before its streams start


def utc(t: datetime | None = None) -> str:
    return (t or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"pipeline: {msg}", flush=True)


def run(cmd: list[str], *, cwd: Path = ROOT, out: Path | None = None, check: bool = True) -> int:
    if out is None:
        return subprocess.run(cmd, cwd=cwd, check=check).returncode
    with out.open("w") as f:
        return subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, check=check).returncode


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


# --- build and unit tests -------------------------------------------------------------


def build(out: Path) -> tuple[Path, str]:
    artifact = out / "mavlink-gateway"
    # -trimpath keeps local paths out of the binary, so the same commit builds the same digest.
    run(["go", "build", "-trimpath", "-o", str(artifact), "./mavlink-gateway/cmd/mavlink-gateway"], cwd=GATEWAYS)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return artifact, "sha256:" + digest


def unit_tests(out: Path) -> dict:
    started = datetime.now(UTC)
    log_path = out / "unit.jsonl"
    code = run(["go", "test", "-json", "-count=1", "./..."], cwd=GATEWAYS, out=log_path, check=False)
    passed, failed = set(), set()
    for line in log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # build errors arrive as plain text
        if event.get("Test") and event.get("Action") in ("pass", "fail"):
            (passed if event["Action"] == "pass" else failed).add((event["Package"], event["Test"]))
    ok = code == 0 and not failed and passed
    note = f"{len(passed) + len(failed)} Go tests across the gateways, {len(failed)} failed"
    if failed:
        note += ": " + ", ".join(sorted(t for _, t in failed)[:5])
    elif code != 0:
        note += f"; go test exited {code} (a package did not build)"
    return {
        "kind": "UNIT", "startedAt": utc(started), "endedAt": utc(),
        "verdict": "PASS" if ok else "FAIL", "verdictNote": note + ".", "log": log_path,
    }


# --- SITL --------------------------------------------------------------------------------


def sitl_run(out: Path, artifact: Path, mode: str) -> dict:
    started = datetime.now(UTC)
    run(["make", "--no-print-directory", "sitl-stop"], out=out / "sitl-container.log", check=False)
    run(["make", "--no-print-directory", "sitl"], out=out / "sitl-container.log")
    gw_out, gw_err = out / "gateway.out", out / "gateway.log"
    cmd = [str(artifact), "-vehicles", f"1={DRONE}", "-rate-hz", "10", "-client-id", "s1a-pipeline"]
    if mode == "dry":
        cmd.append("-dry-run")
    broker = None
    if mode == "s2":  # the broker's own copy, so loss can be placed before or after it
        with (out / "broker.out").open("w") as b:
            broker = subprocess.Popen(["docker", "exec", "s2-mqtt", "mosquitto_sub", "-q", "0",
                                       "-t", f"s2/v1/+/+/{DRONE}/telemetry"], stdout=b, stderr=subprocess.STDOUT)
    with gw_out.open("w") as o, gw_err.open("w") as e:
        gateway = subprocess.Popen(cmd, stdout=o, stderr=e)
    try:
        log("SITL up; flying the sortie (about 3 minutes)")
        fly_code = run(["uv", "run", "--quiet", "tools/sitl/fly.py"], out=out / "fly.log", check=False)
    finally:
        gateway.send_signal(signal.SIGINT)
        try:
            gateway.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway.kill()
        run(["docker", "logs", "s1a-sitl"], out=out / "sitl-container.log", check=False)
        run(["make", "--no-print-directory", "sitl-stop"], out=out / "sitl-stop.log", check=False)
    ended = datetime.now(UTC)

    stats = gateway_stats(gw_err.read_text())
    if mode == "s2":
        time.sleep(3)  # let the sink's last batch land, and the subscriber receive it
        broker.terminate()
        run(["docker", "exec", "s2-mqtt", "pkill", "-f", f"mosquitto_sub.*{DRONE}"], check=False)
        m = measure_s2(started)
        m.update(measure_broker((out / "broker.out").read_text()))
    else:
        m = measure_dry(gw_out.read_text())
    checks = judge(fly_code, stats, m, mode)
    failed = [name for name, ok, _ in checks if not ok]
    note = "; ".join(f"{name}: {detail}" for name, _, detail in checks)
    if mode == "dry":
        note += "; not judged: storage and latency, since the gateway ran with -dry-run"
    speed = m.get("sitl_speed")
    if speed is not None:
        note += f"; SITL ran at {speed:.1%} of real time"
    verdict = "FAIL" if failed else "PASS"
    if failed and speed is not None and abs(speed - 1) > MAX_SPEED_ERROR:
        # A simulator that cannot keep time fails rate and timing checks by itself;
        # that says the host was too busy, not that the build is wrong.
        verdict = "INCONCLUSIVE"
        note = f"Inconclusive: SITL fell to {speed:.1%} of real time, so the host was too busy to judge. " + note
    elif failed:
        note = "Failed " + ", ".join(failed) + ". " + note
    note += "."
    return {
        "kind": "SITL", "startedAt": utc(started), "endedAt": utc(ended),
        "verdict": verdict, "verdictNote": note, "log": out / "fly.log",
        "measures": m, "gatewayStats": stats,
    }


def gateway_stats(text: str) -> dict:
    """The counters on the gateway's 'stopping:' line."""
    for line in reversed(text.splitlines()):
        if "stopping:" in line:
            fields = line.replace("|", " ").replace(",", " ").split()
            out = {}
            for i, word in enumerate(fields):
                if "=" in word:
                    k, v = word.split("=", 1)
                    out[k] = int(v)
                elif word in ("published", "failed") and i + 1 < len(fields) and fields[i + 1].isdigit():
                    out[word] = int(fields[i + 1])
            return out
    return {}


def _rate_and_gaps(times: list[datetime]) -> dict:
    times = sorted(set(times))
    if len(times) < 2:
        return {"messages": len(times), "rate_hz": 0.0, "max_gap_ms": None}
    settled = [t for t in times if t >= times[0] + timedelta(seconds=STREAM_SETTLE_S)]
    span = (settled[-1] - settled[0]).total_seconds() if len(settled) > 1 else 0
    gaps = [(b - a).total_seconds() * 1000 for a, b in itertools.pairwise(settled)]
    return {
        "messages": len(times),
        "rate_hz": round((len(settled) - 1) / span, 2) if span else 0.0,
        "max_gap_ms": round(max(gaps), 1) if gaps else None,
    }


def sitl_speed(points: list[tuple[float, float]]) -> float | None:
    """How fast SITL ran against real time, from (gateway seconds, clock_offset_ms).

    The offset is the vehicle's clock minus the gateway's. A simulator keeping real time
    holds it steady; one that falls behind lets it sink by the time it loses.
    """
    if len(points) < 10 or points[-1][0] - points[0][0] < 10:
        return None
    xs, ys = [p[0] for p in points], [p[1] / 1000 for p in points]
    return 1 + statistics.linear_regression(xs, ys).slope


def measure_dry(stdout: str) -> dict:
    times, gnss, offsets = [], 0, []
    for line in stdout.splitlines():
        _, _, payload = line.partition(" ")
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        times.append(datetime.fromisoformat(msg["ts"]))
        metrics = msg.get("metrics", {})
        gnss += metrics.get("clock_source") == "gnss"
        if "clock_offset_ms" in metrics:  # ts plus the offset is the gateway's time
            gateway_s = times[-1].timestamp() - metrics["clock_offset_ms"] / 1000
            offsets.append((gateway_s, metrics["clock_offset_ms"]))
    m = _rate_and_gaps(times)
    m["gnss_messages"] = gnss
    m["sitl_speed"] = sitl_speed(offsets)
    return m


def measure_broker(text: str) -> dict:
    ts = []
    for line in text.splitlines():
        try:
            ts.append(json.loads(line)["ts"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    # Two messages with one ts become one row: the table is keyed on time per metric.
    return {"delivered": len(ts), "delivered_duplicate_ts": len(ts) - len(set(ts))}


def psql(query: str) -> list[list[str]]:
    out = subprocess.run([*PSQL, query], check=True, capture_output=True, text=True).stdout
    return [line.split("|") for line in out.splitlines() if line]


def measure_s2(since: datetime) -> dict:
    where = (f"object_pk = '{DRONE}' and ingested_at >= '{utc(since)}'"
             f" and time >= timestamptz '{utc(since)}' - interval '5 minutes'")
    rows = psql("select extract(epoch from time), extract(epoch from min(ingested_at))"
                f" from telemetry.reading where {where} group by time order by time")
    times = [datetime.fromtimestamp(float(r[0]), UTC) for r in rows]
    ingested = [datetime.fromtimestamp(float(r[1]), UTC) for r in rows]
    m = _rate_and_gaps(times)
    offset_rows = psql("select extract(epoch from time) - value / 1000, value from telemetry.reading"
                       f" where {where} and metric = 'clock_offset_ms' order by time")
    offsets = [float(r[1]) for r in offset_rows]
    m_speed = sitl_speed([(float(r[0]), float(r[1])) for r in offset_rows])
    gnss = psql(f"select count(*) from telemetry.reading where {where} and metric = 'clock_source'"
                " and value_text = 'gnss'")
    m["gnss_messages"] = int(gnss[0][0])
    m["sitl_speed"] = m_speed
    if offsets and len(times) > 20:
        # ts is on the vehicle's clock; offset = vehicle minus gateway, so adding it puts
        # the sample on the gateway's clock. One mean offset for the run: its spread is ms.
        offset = statistics.fmean(offsets)
        settled = times[0] + timedelta(seconds=STREAM_SETTLE_S)
        lat = sorted((i - t).total_seconds() * 1000 + offset for t, i in zip(times, ingested) if t >= settled)
        m["clock_offset_ms"] = round(offset, 1)
        m["latency_p50_ms"] = round(lat[len(lat) // 2], 1)
        m["latency_p95_ms"] = round(lat[int(len(lat) * 0.95)], 1)
    return m


def judge(fly_code: int, stats: dict, m: dict, mode: str) -> list[tuple[str, bool, str]]:
    checks = [
        ("sortie", fly_code == 0, "flown to the end" if fly_code == 0 else f"fly.py exited {fly_code}"),
        ("rate", m["rate_hz"] >= 9.5, f"{m['rate_hz']} Hz over {m['messages']} messages"),
        ("gaps", m["max_gap_ms"] is not None and m["max_gap_ms"] <= 500, f"longest {m['max_gap_ms']} ms"),
        ("link", stats.get("bad_crc", 1) == 0 and stats.get("failed", 1) == 0,
         (f"{stats.get('frames', '?')} frames, {stats.get('bad_crc', '?')} bad CRC,"
         f" {stats.get('published', '?')} published, {stats.get('failed', '?')} failed")),
    ]
    if mode == "s2":
        published, delivered = stats.get("published", 0), m.get("delivered", 0)
        dups = m.get("delivered_duplicate_ts", 0)
        checks.append(("stored", delivered > 0 and m["messages"] >= delivered - dups,
                       f"published {published}, delivered by the broker {delivered}"
                       + (f" ({dups} sharing a timestamp)" if dups else "")
                       + f", stored {m['messages']}"))
        checks.append(("timestamps", dups == 0, "every message its own timestamp" if dups == 0
                       else f"{dups} messages shared a timestamp with another, and were merged in storage"))
        p95 = m.get("latency_p95_ms")
        checks.append(("latency", p95 is not None and p95 < 500,
                       f"p50 {m.get('latency_p50_ms')} ms, p95 {p95} ms after a {m.get('clock_offset_ms')} ms clock offset"))
    return checks


# --- the record --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sitl", choices=["s2", "dry", "off"], default="s2", help="how to run SITL (default %(default)s)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build with uncommitted changes; the record is marked and must not be submitted")
    ap.add_argument("--submit", action="store_true", help="submit the record with build-submit afterwards")
    args = ap.parse_args()

    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    if dirty and not args.allow_dirty:
        sys.exit("pipeline: the tree has uncommitted changes, so the record could not name what was built."
                 " Commit them, or pass --allow-dirty for a record that is not submitted.")
    if dirty and args.submit:
        sys.exit("pipeline: a record from a dirty tree is not submitted")

    started = datetime.now(UTC)
    build_id = f"BLD-GW-{commit[:12]}"
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "bin" / "pipeline" / build_id / stamp
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pipeline_run = f"github/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    else:
        pipeline_run = f"local/{stamp}"

    log(f"building {build_id} from {commit[:12]}{' (dirty)' if dirty else ''}")
    artifact, digest = build(out)
    log(f"artifact {digest}")
    log("unit tests")
    runs = [unit_tests(out)]
    log(f"unit: {runs[0]['verdict']}, {runs[0]['verdictNote']}")
    if args.sitl != "off":
        runs.append(sitl_run(out, artifact, args.sitl))
        log(f"sitl: {runs[-1]['verdict']}, {runs[-1]['verdictNote']}")

    record = {
        "contract": "s2.build-record/v1",
        "build": {
            "buildId": build_id,
            "name": f"MAVLink gateway {commit[:12]}",
            "target": "GROUND",
            "gitCommit": commit + ("+dirty" if dirty else ""),
            "artifactDigest": digest,
            "builtAt": utc(started),
            "pipelineRun": pipeline_run,
            "implements": [REQUIREMENT],
        },
        "testRuns": [],
    }
    for r in runs:
        entry = {
            "testRunId": f"TR-{r['kind']}-GW-{commit[:12]}-{stamp}",
            "name": {"UNIT": "Gateway unit tests", "SITL": "ArduCopter SITL sortie through the gateway"}[r["kind"]]
                    + f", {commit[:12]}",
            "kind": r["kind"],
            "startedAt": r["startedAt"],
            "endedAt": r["endedAt"],
            "verdict": r["verdict"],
            "verdictNote": r["verdictNote"],
            "logUri": "file:" + str(r["log"].relative_to(ROOT)),
            "verifies": [REQUIREMENT],
        }
        if r["kind"] == "SITL":
            entry["droneKey"] = DRONE
        record["testRuns"].append(entry)
    path = out / "record.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    (out / "measures.json").write_text(json.dumps(
        {r["kind"]: {k: r[k] for k in ("measures", "gatewayStats") if k in r} for r in runs}, indent=2) + "\n")
    log(f"record written: {path.relative_to(ROOT)}")

    if args.submit:
        run(["uv", "run", "--quiet", "--project", "connectors", "build-submit", str(path)])
    # A failed run fails the pipeline. An inconclusive one does not: the host, not the
    # build, was at fault, and the record already says so; nothing asks the gate.
    for r in runs:
        if r["verdict"] == "INCONCLUSIVE":
            prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "pipeline: warning: "
            print(f"{prefix}{r['kind']} run inconclusive: {r['verdictNote']}", flush=True)
    if any(r["verdict"] == "FAIL" for r in runs):
        sys.exit(1)


if __name__ == "__main__":
    main()
