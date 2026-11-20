# connectors

Each connector reads one document produced by a tool outside S2 and writes it into
[Samanvaya 2](https://github.com/veerendra-saraswathi/S2) as objects, links and governed
actions.

| Connector | Reads | Contract | Writes |
|---|---|---|---|
| `hils-submit` | A HILS or actuator bench run | `s2.bench-report/v1` | `TestRun`, its links, `Anomaly` objects, the verdict, and the build's bench step when a HILS run passes |
| `build-submit` | A CI pipeline build | `s2.build-record/v1` | `SoftwareBuild`, `implements` links, the unit and SITL runs with their verdicts, and the build's simulation step when all of them pass |

Together they are the two ends of the chain a drone company has to keep: what was built,
and how it behaved on hardware.

## Use

From the repository root:

```bash
make submit-bench-dry
make submit-bench
make submit-bench REPORT=runs/TR-HILS-0061.json

make submit-build-dry
make submit-build
make submit-build RECORD=builds/BLD-FC-2026.11.5.json

make gate
```

`make gate` submits the worked build record and then `examples/TR-HILS-0059.json`, a HILS
run that passed, which takes build 2026.11.4 from recorded to bench passed.

Directly:

```bash
uv run --project connectors hils-submit  REPORT.json [--api URL] [--dry-run]
uv run --project connectors build-submit RECORD.json [--api URL] [--dry-run]
```

Apply the ontology pack first (`make seed` at the root). The connectors need the
`drone-engineering` pack at **0.3.0 or later**, the version that added `advanceBuild`,
and S2's OPA must be serving `policies/s2/actions/build_gate.rego`. `make seed` does both:
it copies the policy into the S2 checkout at `S2_DIR` (`../S2` unless set) and applies
the pack.

## The contracts

[`schema/`](schema) holds both, and [`examples/`](examples) a worked document for each.
`contract` must name a version the connector knows; anything else is refused rather than
guessed at. Every enumeration mirrors the `drone-engineering` pack, so changing one there
is a breaking change here.

The two contracts overlap on test runs and deliberately differ on what they allow. A
build record's runs may only be `UNIT`, `SITL` or `SIMULATION`, and carry no `bench`
field: a HILS run does not come out of CI, and reporting one there is an error worth
catching. A bench report, conversely, carries anomalies; a failing unit test is not an
`Anomaly`, whose fault classes are physical.

## How a document becomes calls

`plan()` is pure in both modules — a document in, the exact sequence of calls out. That
is what `--dry-run` prints and what the tests assert, so the mapping is covered without
an engine. The shared mappings live in `model.py` so a run reported by a bench and a run
reported by a pipeline become the same thing in S2.

Decisions worth knowing about:

**A run is written as `RUNNING`, never with its verdict**, even when the reporter says it
finished. `status` and `verdict` belong to `recordTestVerdict`; a PUT that set them would
put a governed state into an ordinary write, which is what the ontology exists to prevent.

**A build is created, not replaced.** The create carries `If-None-Match: *` and tolerates
the 409 that means "already recorded". A plain PUT would drag a build that has since
reached `SIM_PASSED` back to `BUILT`, because a PUT replaces the whole object. A `PATCH`
then refreshes the metadata without touching `status`.

**Anomalies are raised before the verdict is recorded**, so a closed run is never left
with its anomalies missing, and the verdict note can refer to them.

**Every action carries a rationale** naming the document and, for builds, the pipeline
run. S2 shows it to an approver and keeps it in the audit log, which is the only reason
an auditor can get from a state change back to the bench or the pipeline that caused it.

## The build gate

A build moves `BUILT` → `SIM_PASSED` → `BENCH_PASSED`, and only then can
`clearBuildForFlight` be requested. Each step is the `advanceBuild` action, asked for by
the connector that saw the run pass:

| Document | Asks for | When |
|---|---|---|
| Build record | `SIM_PASSED` | It holds a passed `SITL` or `SIMULATION` run and no run in it failed or was inconclusive |
| Bench report | `BENCH_PASSED` | Its run is `HILS`, passed, and names a build |

Unit runs earn nothing by themselves, an actuator bench exercises a part and not the
build, and a flight comes after clearance.

**The connector asks; the policy decides.** A connector cannot know what state a build is
in, and should not: `policies/s2/actions/build_gate.rego` allows a step only from the
state before it. A HILS pass on a build that never passed simulation is refused, as is a
second run asking for a step already taken. S2 answers 403 with the reason, the
connector prints it as `gate held` and exits 0: the verdict was recorded, and the gate
holding is an answer, not a failure.

**The step is asked for last**, after every verdict in the document is recorded, so the
gate is never asked about a run whose verdict is not yet in S2.

## Re-submitting

Benches and pipelines re-send. Every action carries an `Idempotency-Key`, so a second
submission replays instead of duplicating, and a `409` from `raiseAnomaly` — S2's answer
when the anomaly exists and nothing changed — is reported and stepped over.

The verdict's key includes the verdict itself, so a reporter that *corrects* a verdict
sends a new request rather than replaying the old one.

A gate step's key is the build, the stage and the run, so a re-sent document replays its
own step and never asks twice.

## Tests

```bash
make test-connectors
```

63 tests over contract validation, both mappings, the build gate, and the submission
behaviour against a stubbed engine. `make test-policy` runs the gate's 21 policy tests,
and needs `opa` and an S2 checkout for the baseline rules they build on.

## Known limits

- **The gate has not run against a live engine.** The policy is tested under OPA and the
  connectors against a stubbed engine; `make seed` and `make gate` on a running stack
  are the check still owed.
- **The policy cannot see the evidence.** S2 shows a policy the build, not the run named
  in `testRunId`. That the run passed and tested this build is the connector's claim,
  kept in the audit log. Checking it in the policy needs S2 to pass linked objects in.
- **The policy lives in this repository and is served from S2's.** A pack cannot carry a
  policy yet, so `make install-policy` copies the file across. Until it is there every
  gate request is denied, which is the safe way round and an easy one to trip over.
- **A failed run does not move a build back.** A build that reached `BENCH_PASSED` and
  then fails a later bench run stays where it is; `rejectBuild` is a person's call.
- **Object writes carry no provenance.** S2's `ObjectWrite` accepts a `provenance` block,
  but it is shaped for the lakehouse — a dataset path and a numeric Iceberg snapshot id —
  and a CI pipeline has neither. Actions carry the source in their rationale instead. A
  CI-shaped provenance type in S2 would be the better answer.
- **The dev realm's `admin` and `operator` users**, over the password grant, as the rest
  of this repo does. A bench or a runner should hold a service account of its own with
  only the roles it needs.
- **The schemas are read from the source tree**, so the connectors run from a checkout.
  They are published contracts and live where people can read them.
- **One document per invocation.** No batch mode, no directory watching.
- **The token-refresh retry is duplicated** from `tools/seed-chain/seed.py` rather than
  shared, to keep that script standalone under PEP 723.
