# /// script
# requires-python = ">=3.10"
# dependencies = ["pymavlink>=2.4"]
# ///
"""Synthetic MAVLink fleet over UDP, for exercising mavlink-gateway without an autopilot.

Each drone is its own MAVLink system (system id 1..N) flying a circle near Hyderabad.
It sends what an ArduPilot or PX4 vehicle sends to a ground station: HEARTBEAT and
SYS_STATUS at 1 Hz, position and attitude at 10 Hz.

    uv run tools/mavlink-sim/sim.py                      # 3 drones to 127.0.0.1:14550
    uv run tools/mavlink-sim/sim.py --drones 5 --seconds 30

Without uv:  pip install pymavlink && python tools/mavlink-sim/sim.py
"""

from __future__ import annotations

import argparse
import math
import os
import time

os.environ.setdefault("MAVLINK20", "1")  # speak MAVLink 2, as current autopilots do

from pymavlink import mavutil  # noqa: E402

BASE_LAT, BASE_LON = 17.4401, 78.3489  # Hyderabad, same origin as S2's telemetry-sim
GROUND_AMSL_M = 540.0
MAV = mavutil.mavlink


class Drone:
    def __init__(self, sysid: int, target: str) -> None:
        self.sysid = sysid
        self.link = mavutil.mavlink_connection(f"udpout:{target}", source_system=sysid, source_component=1)
        self.radius_m = 150.0 + 60.0 * sysid
        self.speed_mps = 8.0 + sysid
        self.height_m = 40.0 + 15.0 * sysid
        self.battery_v = 25.2 - 0.1 * sysid
        self.angle = sysid * 1.3

    def step(self, dt: float, boot_ms: int, slow: bool) -> None:
        mav = self.link.mav
        self.angle += self.speed_mps / self.radius_m * dt
        north, east = self.radius_m * math.cos(self.angle), self.radius_m * math.sin(self.angle)
        lat = BASE_LAT + north / 111_320
        lon = BASE_LON + east / (111_320 * math.cos(math.radians(BASE_LAT)))
        heading = (math.degrees(self.angle) + 90) % 360  # tangent to the circle
        vn = self.speed_mps * math.cos(math.radians(heading))
        ve = self.speed_mps * math.sin(math.radians(heading))

        mav.global_position_int_send(
            boot_ms,
            int(lat * 1e7),
            int(lon * 1e7),
            int((GROUND_AMSL_M + self.height_m) * 1000),
            int(self.height_m * 1000),
            int(vn * 100),
            int(ve * 100),
            0,
            int(heading * 100),
        )
        mav.attitude_send(boot_ms, math.radians(12), math.radians(-2), math.radians(heading), 0, 0, 0.05)
        mav.vfr_hud_send(self.speed_mps + 1.5, self.speed_mps, int(heading), 55, GROUND_AMSL_M + self.height_m, 0)

        if slow:
            self.battery_v = max(21.0, self.battery_v - 0.002)
            remaining = int((self.battery_v - 21.0) / 4.2 * 100)
            mav.heartbeat_send(
                MAV.MAV_TYPE_QUADROTOR,
                MAV.MAV_AUTOPILOT_ARDUPILOTMEGA,
                MAV.MAV_MODE_FLAG_SAFETY_ARMED | MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                3,  # ArduCopter AUTO
                MAV.MAV_STATE_ACTIVE,
            )
            mav.sys_status_send(0, 0, 0, 230, int(self.battery_v * 1000), 1250, remaining, 0, 0, 0, 0, 0, 0)
            mav.gps_raw_int_send(boot_ms * 1000, 3, int(lat * 1e7), int(lon * 1e7), 0, 80, 120, 0, 0, 14)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default="127.0.0.1:14550", help="host:port the gateway listens on")
    parser.add_argument("--drones", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl-C)")
    args = parser.parse_args()
    if not 1 <= args.drones <= 250:
        parser.error("--drones must be 1-250")

    drones = [Drone(sysid, args.target) for sysid in range(1, args.drones + 1)]
    print(f"{len(drones)} simulated drone(s) -> udp {args.target}  (Ctrl-C to stop)")
    start, tick, dt = time.monotonic(), 0, 0.1
    try:
        while not args.seconds or time.monotonic() - start < args.seconds:
            boot_ms = int((time.monotonic() - start) * 1000)
            for drone in drones:
                drone.step(dt, boot_ms, slow=tick % 10 == 0)
            tick += 1
            time.sleep(max(0.0, start + tick * dt - time.monotonic()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
