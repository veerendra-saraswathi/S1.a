# ArduCopter SITL: the real ArduPilot flight code, flying a simulated quadcopter.
#
# The binary is ArduPilot's own stable SITL build for Linux x86_64, statically linked,
# so the image needs nothing but a shell to resolve the gateway's address. Both
# downloads are pinned by SHA-256; a changed file fails the build rather than running.
#
#   docker build -t s1a-sitl-copter tools/sitl        (or: make sitl-image)
#
# To move to a newer stable build, update both URLs and digests together. The params
# file comes from the commit the binary was built from, which
# https://firmware.ardupilot.org/Copter/stable/SITL_x86_64_linux_gnu/git-version.txt names.
FROM debian:bookworm-slim

# ArduCopter stable, built 2026-11-19 from ardupilot@d9c5097c.
ADD --checksum=sha256:0cab7ddc9e9f1ef7f7c88138fcf925824fb952eb15e4f69cd6c8ef9210e25bf0 \
    https://firmware.ardupilot.org/Copter/stable/SITL_x86_64_linux_gnu/arducopter /sitl/arducopter
# ArduPilot's default SITL parameters for a copter, from the same commit.
ADD --checksum=sha256:5e01345b45d1c6190b28bece5638bbdd4cf1cce35e05bbbf480ab24d2b51aa0e \
    https://raw.githubusercontent.com/ArduPilot/ardupilot/d9c5097c3d8333435712ffed86bca8ce00c4bb46/Tools/autotest/default_params/copter.parm /sitl/copter.parm
COPY streams.parm entrypoint.sh /sitl/
# ADD saves a downloaded file as 0600, which the unprivileged user could not read.
RUN chmod 0755 /sitl/arducopter /sitl/entrypoint.sh \
 && chmod 0644 /sitl/copter.parm /sitl/streams.parm \
 && useradd --system --home-dir /var/lib/sitl --create-home sitl

USER sitl
WORKDIR /var/lib/sitl
# SERIAL1: the control port the flight script connects to. SERIAL2: a second telemetry
# port, for the gateway's TCP input. SERIAL0 sends to the gateway over UDP.
EXPOSE 5762 5763
ENTRYPOINT ["/sitl/entrypoint.sh"]
