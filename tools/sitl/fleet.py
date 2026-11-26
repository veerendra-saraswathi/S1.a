# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Fly a small ArduCopter SITL fleet at once through one gateway, and check each drone in S2.

Needs `make sitl-fleet` (the copters) and `make sitl-fleet-gw` (the gateway) running, and
S2 up. Copter i is MAVLink system i, publishes as DRN-000i, and is commanded on TCP
5762 + 10 * (i - 1).

    uv run tools/sitl/fleet.py --fleet 3              # fly all three, then check
    uv run tools/sitl/fleet.py --fleet 3 --reboot 2   # and reboot copter 2 after landing

What the Python simulator cannot show and a fleet of real autopilots can: several
independent boot clocks and GNSS clocks, a vehicle rebooting while others carry on, and
telemetry from many system ids through one UDP port. Per drone, from the broker's own copy
of what the gateway published and from TimescaleDB:

    sortie      fly.py flew it to the end
    rate        at least 9.5 messages a second once its streams start
    gaps        none over 500 ms, apart from a reboot
    order       its ts only moves forward, except where its clock source changes
    distinct    no two of its messages share a timestamp
    identity    every position it reported lies near its own home, not another copter's
    stored      every message the broker delivered for it is in TimescaleDB
    latency     vehicle sample to stored row under 500 ms at p95, by each message's offset
    reboot      the rebooted copter reported BOOT again and its telemetry resumed

A drone whose SITL fell more than 1% off real time, and failed only on timing, is
INCONCLUSIVE: the host, not the gateway, was too slow. The report goes to bin/fleet/.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "pipeline"))
from pipeline import (  # the pipeline's SITL measures, so a drone is judged the same way
    MAX_SPEED_ERROR,
    STREAM_SETTLE_S,
    _rate_and_gaps,
    psql,
    sitl_speed,
    utc,
)

BASE_LAT, BASE_LON, NORTH_STEP_DEG = 17.4401, 78.3489, 0.0016  # as `make sitl-fleet` places them
EARTH_M = 6_378_137.0
TIMING = {"rate", "gaps", "latency"}


def log(msg: str) -> None:
    print(f"fleet: {msg}", flush=True)


def drone(i: int) -> str:
    return f"DRN-{i:04d}"


def home(i: int) -> tuple[float, float]:
    return BASE_LAT + NORTH_STEP_DEG * (i - 1), BASE_LON


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    n = math.radians(b[0] - a[0]) * EARTH_M
    e = math.radians(b[1] - a[1]) * EARTH_M * math.cos(math.radians(a[0]))
    return math.hypot(n, e)


def fly(i: int, out: Path) -> int:
    port = 5762 + 10 * (i - 1)
    alt = 30 + 5 * (i - 1)  # each at its own height, as a fleet would be deconflicted
    with (out / f"fly-{i}.log").open("w") as f:
        return subprocess.run(
            ["uv", "run", "--quiet", "tools/sitl/fly.py", "--connect", f"tcp:127.0.0.1:{port}", "--alt", str(alt)],
            cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, check=False,
        ).returncode


def reboot(i: int) -> datetime:
    """Restart copter i's container: a cold boot, its boot clock back at zero."""
    at = datetime.now(UTC)
    subprocess.run(["docker", "restart", f"s1a-sitl-{i}"], check=True, capture_output=True)
    return at


def per_drone_broker(text: str) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for line in text.splitlines():
        topic, _, payload = line.partition(" ")
        try:
            by[topic.split("/")[4]].append(json.loads(payload))
        except (IndexError, json.JSONDecodeError):
            continue
    return by


def check(i: int, fly_code: int, msgs: list[dict], since: datetime, rebooted_at: datetime | None) -> dict:
    key = drone(i)
    checks: list[tuple[str, bool, str]] = [("sortie", fly_code == 0, "flown to the end" if fly_code == 0 else f"fly.py exited {fly_code}")]

    # Order and distinct times, in the order the broker delivered them.
    ts = [datetime.fromisoformat(m["ts"]) for m in msgs]
    sources = [m.get("metrics", {}).get("clock_source") for m in msgs]
    back = [(a, b) for k, (a, b) in enumerate(itertools.pairwise(ts)) if b <= a and sources[k] == sources[k + 1]]
    checks.append(("order", not back, "ts only moved forward on one clock" if not back else f"{len(back)} steps back on one clock"))
    dups = len(ts) - len(set(ts))
    checks.append(("distinct", dups == 0, "every message its own timestamp" if not dups else f"{dups} share a timestamp"))

    # Rate and gaps once streaming. A reboot splits the run in two at the new boot's first
    # BOOT heartbeat, not when the restart was asked for: `docker restart` lets the old boot
    # run for up to 10 s first. The outage between is the reboot; each side is measured
    # from when its streams start.
    split = len(msgs)
    if rebooted_at is not None:
        split = next((k for k, (t, msg) in enumerate(zip(ts, msgs))
                      if t > rebooted_at - timedelta(seconds=5) and msg.get("metrics", {}).get("system_state") == "BOOT"), len(msgs))
    before = sorted(set(ts[:split]))
    after = sorted(set(ts[split:]))
    m = _rate_and_gaps(before)
    gaps_after = _rate_and_gaps(after)["max_gap_ms"] if len(after) > 2 else None
    worst_gap = max(g for g in (m["max_gap_ms"], gaps_after) if g is not None) if m["max_gap_ms"] is not None else None
    checks.append(("rate", m["rate_hz"] >= 9.5, f"{m['rate_hz']} Hz over {len(ts)} messages"))
    checks.append(("gaps", worst_gap is not None and worst_gap <= 500, f"longest {worst_gap} ms" + (" either side of the reboot" if rebooted_at else "")))

    # Identity: positions near this copter's home, and nearer it than any other's.
    own = home(i)
    positions = [(msg["position"]["lat"], msg["position"]["lon"]) for msg in msgs if msg.get("position")]
    far = [p for p in positions if metres(p, own) > 250]
    checks.append(("identity", bool(positions) and not far,
                   f"{len(positions)} positions within 250 m of its home" if not far else f"{len(far)} of {len(positions)} positions over 250 m from its home"))

    # Stored, and latency by each message's own clock offset.
    where = (f"object_pk = '{key}' and ingested_at >= '{utc(since)}'"
             f" and time >= timestamptz '{utc(since)}' - interval '5 minutes'")
    rows = psql("select extract(epoch from time), extract(epoch from min(ingested_at))"
                f" from telemetry.reading where {where} group by time order by time")
    stored = len(rows)
    checks.append(("stored", stored >= len(set(ts)) > 0, f"delivered {len(ts)}, stored {stored}"))
    offsets = {round(float(t), 3): float(v) for t, v in psql(
        f"select extract(epoch from time), value from telemetry.reading where {where} and metric = 'clock_offset_ms'")}
    first = min((float(r[0]) for r in rows), default=0) + STREAM_SETTLE_S
    lat = sorted((float(r[1]) - float(r[0])) * 1000 + offsets[round(float(r[0]), 3)]
                 for r in rows if float(r[0]) >= first and round(float(r[0]), 3) in offsets)
    p95 = round(lat[int(len(lat) * 0.95)], 1) if lat else None
    checks.append(("latency", p95 is not None and p95 < 500,
                   f"p50 {round(lat[len(lat) // 2], 1) if lat else None} ms, p95 {p95} ms over {len(lat)} messages"))

    if rebooted_at is not None:
        booted = split < len(msgs)
        checks.append(("reboot", booted and len(after) > 50,
                       f"BOOT reported again, {len(after)} messages after" if booted else "no BOOT seen after the restart"))

    # The simulator's speed, from the first boot's clock offset only: a reboot restarts it.
    speed = sitl_speed(sorted((t.timestamp() - (msg["metrics"]["clock_offset_ms"] / 1000), msg["metrics"]["clock_offset_ms"])
                              for t, msg in zip(ts[:split], msgs[:split]) if "clock_offset_ms" in msg.get("metrics", {})))
    failed = [name for name, ok, _ in checks if not ok]
    verdict = "FAIL" if failed else "PASS"
    if failed and set(failed) <= TIMING and speed is not None and abs(speed - 1) > MAX_SPEED_ERROR:
        verdict = "INCONCLUSIVE"
    return {"drone": key, "verdict": verdict, "failed": failed, "sitl_speed": speed,
            "checks": {name: {"ok": ok, "detail": detail} for name, ok, detail in checks}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fleet", type=int, default=3, help="copters, 1 to 5 (default %(default)s)")
    ap.add_argument("--reboot", type=int, default=0, help="restart this copter after landing (0: none)")
    ap.add_argument("--settle", type=float, default=75, help="seconds to keep recording after a reboot")
    ap.add_argument("--recheck", type=Path, help="judge a saved run (a bin/fleet/<stamp> directory) again, flying nothing")
    args = ap.parse_args()
    if args.recheck:
        judge((args.recheck / "run.json").resolve().parent)
        return
    if not 1 <= args.fleet <= 5 or not 0 <= args.reboot <= args.fleet:
        sys.exit("--fleet must be 1-5 and --reboot 0 or one of its copters")

    since = datetime.now(UTC)
    out = ROOT / "bin" / "fleet" / since.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True)
    # The broker's own copy, as the read-only s2-monitor; its password is read in the container.
    sub = ("mosquitto_sub -q 0 -v -u s2-monitor -P \"$(cat /mosquitto/secrets/s2-monitor.password)\""
           " -t 's2/v1/+/CommercialDrone/+/telemetry'")
    capture = (out / "broker.out").open("w")
    broker = subprocess.Popen(["docker", "exec", "s2-mqtt", "sh", "-c", sub], stdout=capture, stderr=subprocess.STDOUT)

    rebooted_at = None
    try:
        log(f"flying {args.fleet} copters at once (about 3 minutes)")
        with ThreadPoolExecutor(args.fleet) as pool:
            codes = dict(zip(range(1, args.fleet + 1), pool.map(lambda i: fly(i, out), range(1, args.fleet + 1))))
        log("sorties: " + ", ".join(f"{drone(i)} {'flown' if c == 0 else f'failed ({c})'}" for i, c in codes.items()))
        if args.reboot:
            rebooted_at = reboot(args.reboot)
            log(f"restarted copter {args.reboot}; recording {args.settle:g} s while it boots and the others idle")
            time.sleep(args.settle)
        time.sleep(3)
    finally:
        broker.terminate()
        subprocess.run(["docker", "exec", "s2-mqtt", "pkill", "-f", "mosquitto_sub.*CommercialDrone"], check=False)
        capture.close()

    run = {"since": utc(since), "fleet": args.fleet, "reboot": args.reboot,
           "rebooted_at": rebooted_at.isoformat() if rebooted_at else None, "sorties": codes}
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    judge(out)


def judge(out: Path) -> None:
    """Check every drone of the run recorded in out, and write report.json there."""
    run = json.loads((out / "run.json").read_text())
    since = datetime.fromisoformat(run["since"])
    rebooted_at = datetime.fromisoformat(run["rebooted_at"]) if run["rebooted_at"] else None
    by = per_drone_broker((out / "broker.out").read_text())
    results = [check(i, run["sorties"][str(i)], by.get(drone(i), []),
                     since, rebooted_at if i == run["reboot"] else None)
               for i in range(1, run["fleet"] + 1)]
    (out / "report.json").write_text(json.dumps({**run, "drones": results}, indent=2) + "\n")

    for r in results:
        speed = f"{r['sitl_speed']:.1%}" if r["sitl_speed"] is not None else "?"
        print(f"\n{r['drone']}: {r['verdict']}  (SITL at {speed} of real time)")
        for name, c in r["checks"].items():
            print(f"  {'ok  ' if c['ok'] else 'FAIL'} {name:9} {c['detail']}")
    log(f"report: {(out / 'report.json').relative_to(ROOT)}")
    if any(r["verdict"] == "FAIL" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
