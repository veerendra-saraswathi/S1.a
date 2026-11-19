# One record from requirement to flight: integrating drone engineering data into an operational ontology

*S1.a technical paper, November 2026. Status: early implementation. Section 6 states what has and has not been verified.*

## Abstract

A commercial drone company generates engineering evidence in many disconnected tools: autopilot telemetry, hardware-in-the-loop bench logs, simulation results, requirements documents and build pipelines. Certification and customer audits require that evidence to be traceable from requirement to flight clearance. We describe S1.a, an integration layer that moves this data into Samanvaya 2 (S2), an open-source ontology platform, and a data model that represents the full traceability chain as typed objects and links. The design separates three control loops by latency so that the integration layer can never interfere with flight or bench control, and it places flight-critical decisions behind policy-checked, two-person approval. We report a first implementation: a dependency-free MAVLink gateway verified against a reference encoder and against S2's contract code, and an ontology pack checked against S2's schema. Performance has not yet been measured.

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

S1.a has three kinds of component: gateways that convert a field protocol into S2's telemetry contract, connectors that write engineering events through S2's API, and an ontology pack that defines the model. One gateway (MAVLink), the pack, and a seeding tool exist today. The [architecture document](architecture.md) gives the component and data-flow diagrams.

Latency separates the system into three loops. Hard real-time loops (milliseconds) stay on the aircraft computer and the bench PC. The soft real-time loop (hundreds of milliseconds to seconds) carries telemetry over MQTT and Kafka into a time-series store. The engineering loop (minutes to weeks) lives in the ontology. Only the last two touch S1.a and S2.

## 4. Telemetry gateway

### 4.1 Parsing

The gateway implements a MAVLink reader for protocol versions 1 and 2. For each candidate frame it locates the start marker, reads the header, and computes the X.25 checksum over header and payload, seeded with the per-message `CRC_EXTRA` value that binds the frame to a specific message definition. Frames that fail are discarded and the parser resynchronises one byte later. MAVLink 2 truncates trailing zero bytes of the payload; the parser restores them so decoders can use fixed offsets. Signed frames are accepted with their 13-byte signature skipped. Frames carrying incompatibility flags the parser does not understand are dropped, as the protocol requires.

Seven messages are decoded. Message ids outside that set are skipped by their declared length. Because their `CRC_EXTRA` is unknown, their checksums cannot be verified; this is acceptable over UDP, where datagram boundaries delimit frames, and is a known weakness on a noisy serial stream.

### 4.2 Rate adaptation

An autopilot emits tens of messages per second per vehicle, while fleet monitoring needs about one consolidated reading per second. The gateway holds the latest value of each metric per vehicle and, on a timer, emits one message containing only the metrics updated since the previous emission. Publish rate is thus set by the operator, independent of autopilot stream rates.

Sentinel values are removed at this stage: battery fields the autopilot reports as unmeasured, non-finite floats, an unknown heading, and a latitude and longitude of exactly zero from a receiver without a fix. Heartbeats from components that are not flight controllers are ignored so that a ground station does not appear as a vehicle.

### 4.3 Timestamps

MAVLink's `time_boot_ms` counts from autopilot boot and is not wall-clock time. The gateway stamps each message with its own clock at emission. At 1 Hz this error is small relative to the interval. It is not adequate for the bench use cases in M2 and M6, where signals from several subsystems must be aligned to the millisecond. That requires timestamps taken at the source against a synchronised clock, and is future work.

### 4.4 Delivery

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
| MQTT publishing works and recovers | Published to a broker, broker stopped and restarted, publishing resumed | Pass |
| The ontology pack is well-formed | Loaded with S2's schema classes; action checks replicated from S2's registry | Pass |
| The seed requests are valid | Bodies validated with S2's request models; paths matched against the engine's route table | Pass |
| The pack applies to a live engine | Applied to the Compose stack; object, link and action types registered | Pass |
| The worked example runs end to end | 25 requests against a live engine: 9 objects, 11 links, three actions executed, `clearBuildForFlight` held at 202 | Pass |
| Re-running the seed changes nothing twice | Second run: pack reported unchanged, objects replaced, links no-ops, the anomaly refused with 409, one approval request | Pass |

**Not yet verified:** any run against a real autopilot or SITL; throughput, latency or resource use of the gateway; behaviour on a lossy link. The live-engine checks were made against S2's `phase2-action-executor` branch; the rest were made against its code.

## 7. Limitations and open questions

1. **Certification credit.** The model records who approved what, but an audit trail is evidence, not qualification. Documents and code produced by AI tools under M3 and M4 may require tool qualification under DO-330 before they count towards certification. This needs a regulatory opinion.
2. **Contract stability.** S2 describes its telemetry contract as a placeholder. The gateway's mapping layer isolates the dependency, but the contract will change.
3. **Schema coverage.** Module 1 datasets, Module 5 scenarios and GIS terrain are not yet modelled.
4. **Authentication at the edge.** MAVLink signatures are not verified and the broker in S2's local stack has no access control.
5. **Open source and proprietary assets.** The integration layer and the generic model can be public. Flight software, customer telemetry and trained models cannot. The boundary between a public pack and a private, airframe-specific overlay is not yet defined.

## 8. Roadmap

Next, in order: build the HILS connector so that M2 verdicts and anomalies arrive as actions; add source-timestamped, higher-rate telemetry for bench use; add CAN; then orchestrate the build gate as a durable workflow that advances a build from simulation to bench to an approval request.

## References

- MAVLink Developer Guide: packet serialization, `CRC_EXTRA`, and message signing. https://mavlink.io/en/guide/serialization.html
- MQTT Version 3.1.1, OASIS Standard, 2014.
- RTCA DO-178C, *Software Considerations in Airborne Systems and Equipment Certification*, 2011.
- RTCA DO-330, *Software Tool Qualification Considerations*, 2011.
- Samanvaya 2 architecture decision records, in particular ADR 0005 (actions and outbox), ADR 0006 (audit chain) and ADR 0008 (telemetry path). https://github.com/veerendra-saraswathi/S2
