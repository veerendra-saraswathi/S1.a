#!/bin/sh
# Start ArduCopter SITL with its telemetry port streaming MAVLink to mavlink-gateway.
#
#   GATEWAY_HOST  where the gateway listens (default host.docker.internal, the Mac or PC
#                 running Docker)
#   GATEWAY_PORT  its UDP port (default 14550)
#   SYSID         the vehicle's MAVLink system id (default 1)
#   SITL_HOME     lat,lon,alt_m,heading_deg (default Hyderabad, where tools/mavlink-sim flies)
#   SPEEDUP       simulation speed; 1 is real time, which source timestamps need
#
# SERIAL0 is the telemetry link to the gateway, which only listens. SERIAL1 (TCP 5762)
# is the control link: tools/sitl/fly.py commands the vehicle there. The EEPROM is
# wiped on every start, so each run begins from the same parameters.
set -eu

host=${GATEWAY_HOST:-host.docker.internal}
port=${GATEWAY_PORT:-14550}

# SITL's udpclient takes an IPv4 address; given a hostname it sends nowhere, silently.
addr=$(getent ahostsv4 "$host" | awk 'NR == 1 { print $1 }')
if [ -z "$addr" ]; then
    echo "cannot resolve GATEWAY_HOST=$host to an IPv4 address" >&2
    exit 1
fi
echo "telemetry: SERIAL0 -> udp $addr:$port ($host); control: SERIAL1 on tcp 5762"

exec /sitl/arducopter \
    --model + \
    --speedup "${SPEEDUP:-1}" \
    --home "${SITL_HOME:-17.4401,78.3489,540,0}" \
    --sysid "${SYSID:-1}" \
    --defaults /sitl/copter.parm,/sitl/streams.parm \
    --serial0 "udpclient:$addr:$port" \
    --serial1 tcp:5762 \
    --wipe
