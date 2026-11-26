# /// script
# requires-python = ">=3.10"
# dependencies = ["pymavlink>=2.4"]
# ///
"""Fly one short sortie on ArduCopter SITL, so the gateway has a real flight to carry.

Connects to SITL's control port (SERIAL1, TCP 5762), waits for the autopilot to pass its
pre-arm checks, then: GUIDED, arm, take off, fly a square, RTL, land, disarm. The
telemetry the gateway sees comes from SERIAL0; this script never talks to the gateway.

It commands only a simulator. Before arming it reads SIM_SPEEDUP, a parameter that
exists only in SITL builds, and stops if the vehicle does not have it.

    uv run tools/sitl/fly.py                     # 30 m up, a 100 m square
    uv run tools/sitl/fly.py --alt 50 --side 60
    uv run tools/sitl/fly.py --connect tcp:127.0.0.1:5772    # a second SITL instance
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

os.environ.setdefault("MAVLINK20", "1")

from pymavlink import mavutil

MAV = mavutil.mavlink
EARTH_RADIUS_M = 6_378_137.0
T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


def offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / EARTH_RADIUS_M
    dlon = east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    n = math.radians(lat2 - lat1) * EARTH_RADIUS_M
    e = math.radians(lon2 - lon1) * EARTH_RADIUS_M * math.cos(math.radians(lat1))
    return math.hypot(n, e)


class Vehicle:
    def __init__(self, url: str) -> None:
        self.m = mavutil.mavlink_connection(url, source_system=255, source_component=190)
        hb = self.m.wait_heartbeat(timeout=30)
        if hb is None:
            sys.exit(f"no heartbeat on {url}; is SITL running? (make sitl)")
        log(f"connected to system {self.m.target_system} on {url}")
        self.m.mav.request_data_stream_send(  # SERIAL1 streams nothing until asked
            self.m.target_system, self.m.target_component, MAV.MAV_DATA_STREAM_ALL, 4, 1)

    def require_simulator(self) -> None:
        for _ in range(5):
            self.m.mav.param_request_read_send(self.m.target_system, self.m.target_component, b"SIM_SPEEDUP", -1)
            p = self.m.recv_match(type="PARAM_VALUE", blocking=True, timeout=2)
            if p is not None and p.param_id == "SIM_SPEEDUP":
                log(f"simulator confirmed (SIM_SPEEDUP={p.param_value:g})")
                return
        sys.exit("the vehicle has no SIM_SPEEDUP parameter, so it is not SITL; refusing to fly it")

    def command(self, cmd: int, *params: float, timeout: float = 5) -> bool:
        p = list(params) + [0.0] * (7 - len(params))
        self.m.mav.command_long_send(self.m.target_system, self.m.target_component, cmd, 0, *p)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ack = self.m.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
            if ack is not None and ack.command == cmd:
                return ack.result == MAV.MAV_RESULT_ACCEPTED
        return False

    def set_mode(self, name: str) -> None:
        mode_id = self.m.mode_mapping()[name]
        if not self.command(MAV.MAV_CMD_DO_SET_MODE, MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_id):
            sys.exit(f"mode {name} refused")
        log(f"mode {name}")

    def position(self) -> tuple[float, float, float]:
        p = self.m.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
        if p is None:
            sys.exit("no position from the vehicle")
        return p.lat / 1e7, p.lon / 1e7, p.relative_alt / 1000

    def climb_to(self, alt: float, timeout: float = 60) -> None:
        # Only watch. A position setpoint sent now would replace the takeoff, and
        # ArduCopter's position controller does not lift a landed vehicle.
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.position()[2] >= alt * 0.95:
                return
        sys.exit(f"did not reach {alt:g} m in {timeout:g} s")

    def arm_when_ready(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        last_text = ""
        while time.monotonic() < end:
            if self.command(MAV.MAV_CMD_COMPONENT_ARM_DISARM, 1, timeout=3):
                log("armed")
                return
            while (t := self.m.recv_match(type="STATUSTEXT", blocking=False)) is not None:
                if t.text != last_text:
                    log(f"waiting for pre-arm: {t.text}")
                    last_text = t.text
            time.sleep(2)
        sys.exit(f"not armable after {timeout:g} s")

    def goto(self, lat: float, lon: float, alt: float, tolerance_m: float = 3, timeout: float = 120) -> None:
        end = time.monotonic() + timeout
        next_send = 0.0
        while time.monotonic() < end:
            if time.monotonic() >= next_send:  # resend: a lost setpoint would leave it hovering
                self.m.mav.set_position_target_global_int_send(
                    0, self.m.target_system, self.m.target_component,
                    MAV.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 0b0000_1111_1111_1000,
                    int(lat * 1e7), int(lon * 1e7), alt, 0, 0, 0, 0, 0, 0, 0, 0)
                next_send = time.monotonic() + 2
            plat, plon, palt = self.position()
            if distance_m(plat, plon, lat, lon) < tolerance_m and abs(palt - alt) < 2:
                return
        sys.exit("waypoint not reached in time")

    def wait_disarmed(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hb = self.m.recv_match(type="HEARTBEAT", blocking=True, timeout=2)
            if (hb is not None and hb.get_srcSystem() == self.m.target_system and hb.type != MAV.MAV_TYPE_GCS
                    and not hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED):
                log("landed and disarmed")
                return
        sys.exit(f"still armed after {timeout:g} s")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connect", default="tcp:127.0.0.1:5762", help="SITL control port (default %(default)s)")
    ap.add_argument("--alt", type=float, default=30, help="flight altitude above home, m (default %(default)g)")
    ap.add_argument("--side", type=float, default=100, help="side of the square, m (default %(default)g)")
    ap.add_argument("--arm-timeout", type=float, default=120, help="seconds to wait for pre-arm checks")
    args = ap.parse_args()

    v = Vehicle(args.connect)
    v.require_simulator()
    v.set_mode("GUIDED")
    v.arm_when_ready(args.arm_timeout)
    home_lat, home_lon, _ = v.position()
    if not v.command(MAV.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, args.alt):
        sys.exit("takeoff refused")
    log(f"taking off to {args.alt:g} m")
    v.climb_to(args.alt)
    log(f"at {args.alt:g} m")

    s = args.side
    for i, (n, e) in enumerate([(s, 0), (s, s), (0, s), (0, 0)], 1):
        lat, lon = offset(home_lat, home_lon, n, e)
        v.goto(lat, lon, args.alt)
        log(f"waypoint {i}/4 reached ({n:g} m N, {e:g} m E)")

    v.set_mode("RTL")
    v.wait_disarmed(timeout=180)


if __name__ == "__main__":
    main()
