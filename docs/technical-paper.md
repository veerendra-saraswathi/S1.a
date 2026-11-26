# One record from requirement to flight: integrating drone engineering data into an operational ontology

*S1.a technical paper, November 2026. Status: early implementation. Section 6 states what has and has not been verified.*

## Abstract

A commercial drone company generates engineering evidence in many disconnected tools: autopilot telemetry, hardware-in-the-loop bench logs, simulation results, requirements documents and build pipelines. Certification and customer audits require that evidence to be traceable from requirement to flight clearance. We describe S1.a, an integration layer that moves this data into Samanvaya 2 (S2), an open-source ontology platform, and a data model that represents the full traceability chain as typed objects and links. The design separates three control loops by latency so that the integration layer can never interfere with flight or bench control, and it places flight-critical decisions behind policy-checked, two-person approval. We report a first implementation: a dependency-free MAVLink gateway verified against a reference encoder and against S2's contract code, and an ontology pack checked against S2's schema. On a laptop with simulated drones, end-to-end p95 from vehicle sample to stored row is 293 ms at 100 drones at 10 Hz, inside the 500 ms target.

## 1. Problem

Project Samanvaya specifies six modules: GNSS-denied night navigation (M1), incremental anomaly detection on HILS benches (M2), generated SDLC documentation (M3), an offline agentic software engineering platform (M4), a multi-drone GIS simulator (M5), and a real-time visual twin for actuator faults (M6). Each is specified with its own inputs, outputs and deliverables.

Read together, the specifications describe one process. M3 turns design reviews into requirements and documents. M4 turns those into code and builds. M5 exercises builds in simulation. M2 and M6 exercise them on hardware and report what went wrong. What went wrong changes the requirements. No module specification owns that loop, and none defines the identifiers that would let a test result refer to the build it tested or the requirement it verified.

Two consequences follow. Traceability, which aviation software assurance processes such as DO-178C make mandatory, must be reconstructed by hand. And the loop cannot be automated, because there is no shared state for automation to act on.

## 2. Design principles

**P1. The integration layer is a record, not a data path.** Module 6 must render within 20 ms of the physical actuator. Module 1 must deliver position to the flight controller at 10 Hz or faster. A message bus and a database cannot sit inside those budgets. S1.a therefore observes: it receives copies of data and never stands between a sensor and an actuator. The MAVLink gateway makes this structural by implementing no transmit path.

**P2. One model, written to by every module.** Rather than pairwise integrations between six modules, each writes to a shared ontology. Adding a module adds one writer, not five interfaces.

**P3. Simulated and real data share a contract.** If the simulator emits telemetry in the same form as an aircraft, anomaly models and dashboards built on one work on the other, and a simulated mission is replayable through the same pipeline as a flown one.

**P4. Decisions are actions, and actions are governed.** Changing a build's status to "cleared for flight" is not a database update. It is a named action with validated parameters, a policy decision, an approval by a different person, and an audit entry. Where AI agents participate, as M3 and M4 require, they may propose and never decide.

**P5. Buildable offline.** Module 4 requires zero internet dependency to protect flight-control intellectual property. Components that run inside that boundary must build and run without fetching anything.

## 3. Architecture

S1.a has three kinds of component: gateways that convert a field protocol into S2's telemetry contract, connectors that write engineering events through S2's API, and an ontology pack that defines the model. One gateway (MAVLink), two connectors (bench reports and CI build records), the pack, and a seeding tool exist today. The [architecture document](architecture.md) gives the component and data-flow diagrams.

Latency separates the system into three loops. Hard real-time loops (milliseconds) stay on the aircraft computer and the bench PC. The soft real-time loop (hundreds of milliseconds to seconds) carries telemetry over MQTT and Kafka into a time-series store. The engineering loop (minutes to weeks) lives in the ontology. Only the last two touch S1.a and S2.

## 4. Telemetry gateway

### 4.1 Parsing

The gateway implements a MAVLink reader for protocol versions 1 and 2. For each candidate frame it locates the start marker, reads the header, and computes the X.25 checksum over header and payload, seeded with the per-message `CRC_EXTRA` value that binds the frame to a specific message definition. Frames that fail are discarded and the parser resynchronises one byte later. MAVLink 2 truncates trailing zero bytes of the payload; the parser restores them so decoders can use fixed offsets. Signed frames are accepted with their 13-byte signature skipped. Frames carrying incompatibility flags the parser does not understand are dropped, as the protocol requires.

Seven messages are decoded. Message ids outside that set are skipped by their declared length. Because their `CRC_EXTRA` is unknown, their checksums cannot be verified; this is acceptable over UDP, where datagram boundaries delimit frames, and is a known weakness on a noisy serial stream.

### 4.2 Rate adaptation

An autopilot emits tens of messages per second per vehicle, while fleet monitoring needs about one consolidated reading per second. The gateway holds the latest value of each metric per vehicle and, on a timer, emits one message containing only the metrics updated since the previous emission. Publish rate is thus set by the operator, independent of autopilot stream rates.

Sentinel values are removed at this stage: battery fields the autopilot reports as unmeasured, non-finite floats, an unknown heading, and a latitude and longitude of exactly zero from a receiver without a fix. Heartbeats from components that are not flight controllers are ignored so that a ground station does not appear as a vehicle.

### 4.3 Timestamps

MAVLink's `time_boot_ms` counts from autopilot boot and is not wall-clock time. The gateway keeps a clock per vehicle that puts it on UTC, and stamps each message with when its newest timed sample was taken on the vehicle. The boot epoch comes from SYSTEM_TIME, which pairs the autopilot's GNSS-derived UTC with `time_boot_ms`; without it, from the minimum of arrival minus `time_boot_ms` over a ten-second window, which keeps the samples' spacing exact and puts them on the gateway's clock. Each message names its clock source (`gnss`, `estimated` or `gateway`), and carries the vehicle-to-ground offset whenever SYSTEM_TIME makes it measurable. A backwards step in `time_boot_ms` is a reboot and resets the clock. The gateway cannot use MAVLink TIMESYNC, because that needs it to transmit.

This gives one vehicle's signals millisecond alignment, and several vehicles alignment to the accuracy of their GNSS time. It does not give sub-millisecond agreement between separate bench subsystems in M2 and M6; that needs the subsystems on one PTP-disciplined clock, and stays future work.

### 4.4 The CAN gateway

Bench rigs in M2 and M6 speak DroneCAN on a CAN bus. The CAN gateway reads the bus as Linux can-utils logs it (`candump -L`), so it opens no CAN socket and has no way to transmit, the same observe-only rule as the MAVLink gateway. It reassembles multi-frame transfers per source node and data type, checks the toggle bit and transfer id, and verifies each transfer's CRC-16 over the data type's signature and payload, dropping and counting any transfer that fails. It decodes NodeStatus, ESC status, actuator status and actuator commands, and has been checked against the dronecan reference encoder.

Commands are broadcast by the flight controller, but they are filed with the actuator they address, on the object of the node that reports that actuator, so commanded and measured position arrive in S2 on one object. That pair is what the M6 twin compares. Messages are stamped with the capture's kernel timestamps and cut into windows on capture time, so a recorded bench run uploaded later lands exactly as it happened.

### 4.5 Delivery

Messages are published at MQTT QoS 0. S2's own design accepts loss of periodic telemetry at this hop and makes everything downstream replayable from Kafka. The gateway reconnects on failure and counts what it could not send. Events that must not be lost, such as a bench verdict, do not travel this path; they go through S2's API as actions.

## 5. Data model

The ontology pack defines six object types and nine link types that close the loop described in Section 1:

> Requirement → EngineeringDocument; SoftwareBuild → Requirement; TestRun → SoftwareBuild, Requirement, CommercialDrone; Anomaly → TestRun, Subsystem; Subsystem → CommercialDrone.

Three properties of this model matter.

*Test runs are uniform.* A unit test, a SITL run, a simulated mission, a HILS endurance run and a flight trial are all `TestRun` objects distinguished by `kind`. A requirement's verification status is then one query across all of them.

*Anomalies carry the M6 fault taxonomy.* Reduced gain, binding, shifted natural frequency and increased damping are enumerated fault classes, so a twin's classification is structured data and not free text.

*Telemetry is bound, not copied.* High-rate signals stay in the time-series store. An object type declares which metric a property reads, and S2 resolves it by the object's natural key.

Six actions change state. Four are routine. `baselineDocument` requires approval and may be proposed by an agent, which fits M3's generated documents. `clearBuildForFlight` requires approval by a second person and is closed to agents entirely.

## 6. Verification

| Claim | How it was checked | Result |
|---|---|---|
| The parser decodes MAVLink correctly | Frames for every decoded message, encoded by pymavlink in v1 and v2, decoded and compared field by field | Pass |
| The parser survives fragmentation and noise | A multi-frame stream fed one byte at a time with leading garbage and an undecoded message | Pass |
| Corrupt frames are rejected | A flipped payload byte; the following frame must still parse | Pass |
| Signed and incompatible v2 frames are handled | Constructed frames with the relevant flags | Pass |
| Output conforms to S2's telemetry contract | Gateway output from a simulated three-drone run passed through S2's own `envelope_from_mqtt` | All messages valid |
| Output is telemetry contract v1, frozen | Every message the gateways' tests publish validated against v1's JSON Schema, pinned from S2 and compared with it on every push; 21,381 messages from three SITL fleet runs through the schema and S2's strict bridge | All valid |
| The CAN gateway decodes DroneCAN correctly | Frames for NodeStatus, ESC status, actuator status and ArrayCommand, single- and multi-frame, encoded by the dronecan package and decoded field by field; a flipped byte fails the transfer CRC; a lost frame drops the transfer; interleaved transfers from two nodes both complete | Pass |
| A simulated bench becomes S2 telemetry | 20 s of a gimbal rig (50 Hz commands and status, 10 Hz ESCs, 1 Hz NodeStatus): 8,460 frames, 3,860 transfers, none failed; 420 messages, all valid under S2's `envelope_from_mqtt`. A reduced-gain fault at 10 s took the pitch axis from 1.00 to 0.60 of its command, and the gimbal's health to WARNING | Pass |
| A live bench capture reaches TimescaleDB | The simulator in real time, piped through the gateway to the broker for 12 s: 252 messages stored at 10.00 Hz, stamped with capture time. A binding fault at 6 s raised the stored pitch tracking error from 0.014 to 0.073 rad, capped at the rig's 0.1 rad | Pass |
| The telemetry path's latency is known per hop | `tools/bench`, 3 to 100 simulated drones at 10 Hz, every message timed at the broker, bridge, Kafka and TimescaleDB ([benchmarks](benchmarks.md)): nothing lost; gateway, broker, bridge and Kafka under 150 ms; S2's sink waited up to 1 s per batch, putting p95 at about 1 s against the reference's 500 ms. With the wait cut to 0.1 s and each batch written as one `unnest` insert per table (2.2 times faster in an A/B test), p95 is 146, 175, 246 and 293 ms at 3, 10, 50 and 100 drones | Pass, in simulation |
| Messages carry source time at 10 Hz | Three simulated drones whose clocks run 250, 500 and 750 ms ahead, through the gateway at 10 Hz for 12 s: 120 messages each at 10.1 Hz, every one `gnss`, offsets measured at 249.9, 500.0 and 750.0 ms, spacing 94-104 ms, none backwards, all valid under S2's `envelope_from_mqtt`. Without SYSTEM_TIME, every message `estimated` | Pass |
| Source time survives the S2 pipeline | 8 s of the same run published to the broker: TimescaleDB holds 80 positions per drone at 10.0 Hz, keyed by vehicle time, with the offsets stored. Ingest minus source time was 804, 554 and 304 ms, which with each drone's skew gives one pipeline latency, 1,054 ms | Pass |
| A real autopilot's telemetry reaches S2 | ArduCopter stable SITL (`tools/sitl`) flew one scripted sortie, take off to 30 m, a 100 m square, RTL, through the gateway at 10 Hz into TimescaleDB: 1,745 messages, 19 metrics, 1,816 positions over 175 s, gaps only before the autopilot's streams started. 1,671 messages carried the autopilot's GNSS time. Its clock ran 461.5 ms (σ 5.1) behind the host's; with that added back, vehicle sample to stored row was p50 148 ms and p95 196 ms | Pass |
| A real build is gated on a real SITL run | `tools/pipeline` built the gateway (digest reproduced by a second build), ran its 42 Go tests and flew ArduCopter SITL through that binary, judging the run against SW-TLM-001. The record went through the build connector and the policy moved BLD-GW-e4eee545ac21 to `SIM_PASSED` on the SITL run: 10.04 Hz, 1,568 messages published, delivered and stored, p95 153 ms. On a host loaded to a 15-minute average of 28, the same run found that 15 of 381 messages shared a timestamp and that untimed messages sat 1.7 s ahead of the vehicle's clock; the gateway was changed so one vehicle's time only moves forward | Pass |
| The broker admits only who it should | S2's broker with logins on both listeners, a per-user topic ACL and TLS on 8883 for gateways. The gateway over TLS with its login stored all 360 messages; a wrong password, no login and an untrusted certificate were refused; 360 messages sent to another tenant's topic were dropped and none stored | Pass |
| TLS adds no measurable latency to the gateway's hop | Plain and TLS alternated twice at 50 and 100 drones, with logins: gateway → broker p95 46–100 ms over TLS, 80–1,059 ms plain; nothing delivered by the broker went unstored. On a loaded host (load average up to 27) end-to-end p95 at 100 drones was 0.5–10.7 s in three of four runs in both modes, from the sink and TimescaleDB, so the 500 ms target with TLS stays to be shown on a quiet host | Pass, with the caveat |
| A fleet of autopilots through one gateway | `make sitl-fleet-fly`: three ArduCopter SITL copters flown at once, copter 2 rebooted after landing, judged per drone from the broker's copy and TimescaleDB. All three passed at 100% of real time: 9.7–10.0 Hz, no gap over 200 ms, timestamps distinct and forward, every position within 250 m of its own home, every delivered message stored, p95 170–215 ms. The rebooted copter reported BOOT again and resumed. The gateway's reboot count matched every boot over three runs | Pass |
| MQTT publishing works and recovers | Published to a broker, broker stopped and restarted, publishing resumed | Pass |
| The ontology pack is well-formed | Loaded with S2's schema classes; action checks replicated from S2's registry | Pass |
| The seed requests are valid | Bodies validated with S2's request models; paths matched against the engine's route table | Pass |
| The pack applies to a live engine | Applied to the Compose stack; object, link and action types registered | Pass |
| The worked example runs end to end | At pack 0.4.0, 27 requests against a live engine: 9 objects, 11 links, four actions executed (`advanceBuild` moving BLD-FC-2026.11.3 to `SIM_PASSED` among them), `baselineDocument` held at 202, and `clearBuildForFlight` refused at 403 with the policy's reason, since the HILS run failed | Pass |
| The HILS connector writes a bench run into a live engine | The worked report submitted against the Compose stack: the run created, its build, drone and requirement linked, two anomalies raised with their windows, one attributed and one left open, the verdict recorded | Pass |
| The build connector writes a CI build into a live engine | The worked record submitted against the Compose stack: the build created at BUILT, its metadata refreshed, one requirement implemented, and the unit and SITL runs written, linked and closed PASS | Pass |
| A re-submitted build record cannot move a build backwards | The same record submitted again: the guarded create answered 409 and changed nothing, the PATCH refreshed the metadata without touching status, and the verdicts replayed | Pass |
| A re-submitted document cannot reopen a closed run | Both worked documents re-sent after the fix: each run's guarded create answered 409, the PATCH left status and verdict alone, every run stayed `COMPLETED` with its verdict. Before the fix a re-send had put three runs back to `RUNNING` with no verdict; they were re-recorded through `recordTestVerdict` | Pass |
| The build gate advances a build on a live engine | `make gate`: BLD-FC-2026.11.4 moved `BUILT` to `SIM_PASSED` on its SITL run and to `BENCH_PASSED` on TR-HILS-0059; a second run replayed every step | Pass |
| The gate refuses an out-of-order step on a live engine | `advanceBuild` to `SIM_PASSED` on the `BENCH_PASSED` build: 403, "a build reaches SIM_PASSED from BUILT, and this one is BENCH_PASSED" | Pass |
| A bench-passed build's clearance goes to a second person | `clearBuildForFlight` on BLD-FC-2026.11.4: 202, pending approval | Pass |
| The gate refuses a step the named run did not earn | On a live engine, with S2 showing the policy the run (`policy_context`): `advanceBuild` to `BENCH_PASSED` on BLD-FC-2026.11.3 refused at 403 four times, naming a failed HILS run ("has not passed: it is COMPLETED with verdict FAIL"), another build's run ("did not test this build"), a simulation run ("does not earn BENCH_PASSED") and a missing run. Before the change the failed run had advanced the build | Pass |
| Re-running the seed changes nothing twice | Second run: pack reported unchanged, objects replaced, links no-ops, the anomaly refused with 409, and one pending request per approval-gated action | Pass |

**Not yet verified:** sustained load, since the benchmark steps are 20 s; the 500 ms target at 100 drones with TLS on, which needs a quiet host; the CAN gateway on a real bus, which needs a Linux bench PC; a real flight controller, since only SITL has been run; throughput beyond 100 vehicles; behaviour on a lossy link. The live-engine checks were made against S2's `phase2-action-executor` branch; the rest were made against its code.

## 7. Limitations and open questions

1. **Certification credit.** The model records who approved what, but an audit trail is evidence, not qualification. Documents and code produced by AI tools under M3 and M4 may require tool qualification under DO-330 before they count towards certification. This needs a regulatory opinion.
2. **Contract stability.** S2 froze its telemetry contract as v1 on 2026-11-27: JSON Schema for the topic and payload, and conformance cases both repositories are tested against. v1 does not change; a new field or rule is v2, accepted beside it. What remains open is Samanvaya 0.5: v1 was written without its topics and payloads, so 0.5 either adopts v1 or its form becomes v2.
3. **Schema coverage.** Module 1 datasets, Module 5 scenarios and GIS terrain are not yet modelled.
4. **Authentication at the edge.** MAVLink signatures are not verified and the broker in S2's local stack has no access control.
5. **What the build gate checks.** `advanceBuild` declares the run it names as policy context, so S2 shows the policy that run and the build it tested, and the policy refuses a run that is missing, not `COMPLETED` with verdict `PASS`, of another build, or of a kind that does not earn the step. A live probe before this change had advanced a build to `BENCH_PASSED` on a failed HILS run; the same request is now refused. Two things remain the requester's: that nothing else in a build record failed, which only the connector checks, and that the run's own record is true, which is the bench's and the pipeline's word. Which kinds earn the bench is also a choice: only HILS does, not `ACTUATOR_BENCH`.
6. **Open source and proprietary assets.** The integration layer and the generic model can be public. Flight software, customer telemetry and trained models cannot. The boundary between a public pack and a private, airframe-specific overlay is not yet defined.

## 8. Roadmap

Next, in order: run the CAN gateway on a real bench; then orchestrate the build gate as a durable workflow that advances a build from simulation to bench to an approval request.

## References

- MAVLink Developer Guide: packet serialization, `CRC_EXTRA`, and message signing. https://mavlink.io/en/guide/serialization.html
- MQTT Version 3.1.1, OASIS Standard, 2014.
- RTCA DO-178C, *Software Considerations in Airborne Systems and Equipment Certification*, 2011.
- RTCA DO-330, *Software Tool Qualification Considerations*, 2011.
- Samanvaya 2 architecture decision records, in particular ADR 0005 (actions and outbox), ADR 0006 (audit chain) and ADR 0008 (telemetry path). https://github.com/veerendra-saraswathi/S2
