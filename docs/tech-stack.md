# Tech stack

Every technology S1.a uses or plans to use, and the reason for each choice. "In use" means there is code in this repository that depends on it today.

## In this repository

| Technology | Used for | Status | Why this choice |
|---|---|---|---|
| Go 1.22+ (standard library only) | `mavlink-gateway` | In use | One static binary for bench PCs and ground stations. No module downloads, so it builds on an air-gapped host. |
| MAVLink v1 and v2, common dialect | Vehicle telemetry input | In use | The protocol ArduPilot and PX4 speak. Parsed by our own reader for seven messages; see the trade-off below. |
| MQTT 3.1.1, QoS 0 | Gateway to S2 | In use | S2's ingestion contract is defined on MQTT. QoS 0 matches how S2 treats periodic telemetry. |
| JSON | Telemetry payload | In use | Fixed by S2's telemetry contract v1. |
| YAML ontology pack | `drone-engineering` model | In use | S2's format for defining object, link and action types as reviewable code. |
| JSON Schema (2020-12) | Action parameters | In use | S2 validates action requests against it. |
| Python 3.10+ | Simulator, seed script, golden-frame generator, `hils-connector` | In use | Nothing on the telemetry path. The connector writes engineering events through S2's API, where latency does not matter and a verdict must not be lost. |
| pymavlink | Simulator and reference encoder for parser tests | In use | The reference MAVLink implementation, so the parser is tested against an independent encoder. |
| httpx, PyYAML | Seed script and connector | In use | Same HTTP client S2's own CLI uses. |
| JSON Schema (2020-12) and `jsonschema` | The `s2.bench-report/v1` contract | In use | A bench report is validated before anything is written, so a malformed report is refused rather than half-applied. The schema doubles as the specification M2 writes against. |
| uv | Running Python tools with pinned inline dependencies | In use | Scripts declare their own dependencies (PEP 723); no virtualenv to manage. |
| Make | Task entry points | In use | |
| Mermaid | Diagrams in the docs | In use | Rendered by GitHub; diagrams stay in version control as text. |

### Trade-off: own MAVLink parser and MQTT client

The gateway has no third-party dependencies. The cost is that it supports only what it needs: seven MAVLink messages, and MQTT publishing at QoS 0. Full libraries exist for both (`gomavlib`, `paho.mqtt.golang`) and are the right replacement if the gateway ever needs to send MAVLink, decode a full dialect, or publish at QoS 1. Both are isolated behind small packages (`internal/mavlink`, `internal/mqtt`) so the swap is contained.

## Provided by S2

S1.a depends on these through S2. They are not deployed from this repository.

| Technology | Role in S2 |
|---|---|
| Python 3.13, FastAPI, SQLAlchemy, Pydantic | Ontology engine and ingestion services |
| PostgreSQL 18 with PostGIS | Ontology state and geometry |
| TimescaleDB | Telemetry time series |
| Apache Kafka | Durable telemetry log between the MQTT bridge and the stores |
| Eclipse Mosquitto | MQTT broker |
| Open Policy Agent (Rego) | Policy decisions on actions |
| Keycloak (OIDC) | Identity, roles, tokens |
| SeaweedFS (S3 API) | Object storage |
| React, TypeScript, Vite | Workshop UI |
| Docker Compose, Helm | Local stack and Kubernetes deployment |

## Planned

Nothing in this section exists in the repository yet. Choices marked "proposed" are recommendations still open to change.

| Technology | For | Status | Reasoning |
|---|---|---|---|
| Temporal, self-hosted | Orchestrating the build gate across simulation, bench and approval | Proposed | Durable workflows that survive multi-day waits for approval; runs offline; Python and Go SDKs. Argo Workflows is the alternative if everything should be Kubernetes-native. |
| PTP (IEEE 1588) | Source timestamps for bench telemetry | Proposed | Correlating HILS and actuator signals needs sub-millisecond clock agreement, which gateway-side timestamps cannot give. |
| DroneCAN / CAN | Bench bus telemetry | Planned | Module 2 and Module 6 inputs include CAN. |
| MCAP or Parquet | Raw bench and flight logs in the lakehouse | Proposed | High-rate signals belong in files indexed by test run, not in per-reading rows. |
| Signed build attestations (in-toto / SLSA style) | Linking each `SoftwareBuild` to its commit and pipeline run | Proposed | Makes build provenance verifiable, which certification evidence needs. |
| C++ | Module 1 onboard navigation | From the specification | Real-time localisation on NVIDIA Jetson Orin NX. |
| Unity 3D, C# | Module 5 simulator and Module 6 visual twin | From the specification | Named as preferred in the module specifications. These projects would live in their own repositories and use a thin C# client. |
| On-premises quantised LLMs | Module 3 and Module 4 | From the specification | Module 4 requires zero internet dependency. |

## Versions and compatibility

| Item | Requirement |
|---|---|
| Go | 1.22 or later |
| Python | 3.10 or later for the tools |
| S2 telemetry contract | v1 (`s2/v1/...` topics). S2 documents this contract as a placeholder; a change there means changing `internal/telemetry`. |
| S2 ontology engine | Pack format as on S2's `main` branch, November 2026 |
