# /// script
# requires-python = ">=3.10"
# dependencies = ["dronecan>=1.0.25"]
# ///
"""A simulated DroneCAN bench, written as `candump -L` lines, for exercising can-gateway.

The rig is a two-axis gimbal on a HILS bench:

    node 1   flight controller   ArrayCommand to actuators 3 (pitch) and 4 (yaw), 50 Hz
    node 10  gimbal              actuator Status for 3 and 4 at 50 Hz, a first-order lag
                                 behind the command
    node 20  ESC board           esc Status for 4 ESCs, 10 Hz
    all                          NodeStatus, 1 Hz

--fault injects one of the actuator faults the M6 visual twin classifies, on the pitch
axis, from --fault-at seconds on. It shows in actuator_3_position against
actuator_3_command, and the gimbal's NodeStatus goes to WARNING.

    reduced_gain   the axis reaches 60% of what it is commanded
    binding        the axis sticks: it moves only once the error passes 0.1 rad
    timing_drift   the response lags three times as long

    uv run tools/can-sim/sim.py --seconds 30 > run.log          # as fast as it can, timestamped
    uv run tools/can-sim/sim.py --realtime | ./bin/can-gateway  # live, as candump would be
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import dronecan
from dronecan import uavcan
from dronecan.transport import Transfer

CONTROL_HZ = 50
FC, GIMBAL, ESC = 1, 10, 20


class Bus:
    """Encodes transfers into candump lines with per-(type, node) transfer ids."""

    def __init__(self, out, iface: str) -> None:
        self.out, self.iface, self.tids = out, iface, {}

    def send(self, t: float, node: int, msg, priority: int = 16) -> None:
        key = (type(msg).__name__, node)
        tid = self.tids.get(key, 0)
        self.tids[key] = (tid + 1) % 32
        transfer = Transfer(payload=msg, source_node_id=node, transfer_id=tid, transfer_priority=priority)
        micros = round(t * 1e6)
        for i, frame in enumerate(transfer.to_frames()):
            stamp = micros + 20 * i  # 20 us between frames: 1 Mbit/s, about 130 bits a frame
            self.out.write(
                f"({stamp // 1_000_000}.{stamp % 1_000_000:06d}) {self.iface} "
                f"{frame.message_id:08X}#{bytes(frame.bytes).hex().upper()}\n"
            )


class Axis:
    def __init__(self, actuator_id: int, amplitude: float, period_s: float, tau_s: float) -> None:
        self.id, self.amplitude, self.period, self.tau = actuator_id, amplitude, period_s, tau_s
        self.position = 0.0

    def command(self, elapsed: float) -> float:
        return self.amplitude * math.sin(2 * math.pi * elapsed / self.period)

    def step(self, cmd: float, dt: float, fault: str | None) -> float:
        tau, target = self.tau, cmd
        if fault == "reduced_gain":
            target = 0.6 * cmd
        elif fault == "timing_drift":
            tau = 3 * self.tau
        elif fault == "binding" and abs(cmd - self.position) < 0.1:
            return self.position
        self.position += (target - self.position) * (1 - math.exp(-dt / tau))
        return self.position


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=10, help="length of the run (0 with --realtime: until Ctrl-C)")
    parser.add_argument("--realtime", action="store_true", help="pace output to the clock and stamp it with now")
    parser.add_argument("--start", type=float, default=None, help="Unix time of the first frame (default: now)")
    parser.add_argument("--iface", default="can0")
    parser.add_argument("--fault", choices=["reduced_gain", "binding", "timing_drift"])
    parser.add_argument("--fault-at", type=float, default=5, help="seconds into the run the fault starts")
    args = parser.parse_args()

    _ = dronecan  # the import registers the DSDL types
    bus = Bus(sys.stdout, args.iface)
    pitch, yaw = Axis(3, 0.35, 4.0, 0.05), Axis(4, 0.8, 6.0, 0.08)
    start = args.start if args.start is not None else time.time()
    wall0 = time.monotonic()
    dt = 1 / CONTROL_HZ
    tick = 0
    try:
        while True:
            elapsed = tick * dt
            if args.seconds and elapsed >= args.seconds:
                break
            if args.realtime:
                time.sleep(max(0.0, wall0 + elapsed - time.monotonic()))
            t = start + elapsed
            fault = args.fault if args.fault and elapsed >= args.fault_at else None

            commands = []
            for axis in (pitch, yaw):
                cmd = axis.command(elapsed)
                commands.append(uavcan.equipment.actuator.Command(actuator_id=axis.id, command_type=1, command_value=cmd))
            bus.send(t, FC, uavcan.equipment.actuator.ArrayCommand(commands=commands), priority=8)

            for axis, cmd in ((pitch, commands[0].command_value), (yaw, commands[1].command_value)):
                pos = axis.step(cmd, dt, fault if axis is pitch else None)
                load = 2.0 * abs(cmd - pos) + 0.3
                bus.send(t + 0.0004, GIMBAL, uavcan.equipment.actuator.Status(
                    actuator_id=axis.id, position=pos, force=load, speed=math.nan, power_rating_pct=min(100, int(load * 30)),
                ))

            if tick % (CONTROL_HZ // 10) == 0:
                for i in range(4):
                    bus.send(t + 0.002, ESC, uavcan.equipment.esc.Status(
                        error_count=0, voltage=24.8 - 0.01 * elapsed / 10, current=6.5 + 0.2 * i,
                        temperature=303.15 + 0.05 * elapsed, rpm=4200 + 15 * i, power_rating_pct=38, esc_index=i,
                    ))

            if tick % CONTROL_HZ == 0:
                uptime = int(elapsed) + 600
                for node, health in ((FC, 0), (GIMBAL, 1 if fault else 0), (ESC, 0)):
                    bus.send(t + 0.003, node, uavcan.protocol.NodeStatus(
                        uptime_sec=uptime, health=health, mode=0, sub_mode=0, vendor_specific_status_code=0,
                    ))
            if args.realtime:
                sys.stdout.flush()
            tick += 1
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
