# connectors

Each connector reads one document produced by a tool outside S2 and writes it into
[Samanvaya 2](https://github.com/veerendra-saraswathi/S2) as objects, links and governed
actions.

| Connector | Reads | Contract | Writes |
|---|---|---|---|
| `hils-submit` | A HILS or actuator bench run | `s2.bench-report/v1` | `TestRun`, its links, `Anomaly` objects, the verdict |
| `build-submit` | A CI pipeline build | `s2.build-record/v1` | `SoftwareBuild`, `implements` links, the unit and SITL runs with their verdicts |

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
```

Directly:

```bash
uv run --project connectors hils-submit  REPORT.json [--api URL] [--dry-run]
uv run --project connectors build-submit RECORD.json [--api URL] [--dry-run]
```

Apply the ontology pack first (`make seed` at the root). The connectors need the
`drone-engineering` pack at **0.2.0 or later**, the version that added the anomaly
window to `raiseAnomaly`.

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

## Re-submitting

Benches and pipelines re-send. Every action carries an `Idempotency-Key`, so a second
submission replays instead of duplicating, and a `409` from `raiseAnomaly` — S2's answer
when the anomaly exists and nothing changed — is reported and stepped over.

The verdict's key includes the verdict itself, so a reporter that *corrects* a verdict
sends a new request rather than replaying the old one.

## Tests

```bash
make test-connectors
```

44 tests over contract validation, both mappings, and the submission behaviour against a
stubbed engine.

## Known limits

- **`SIM_PASSED` and `BENCH_PASSED` cannot be set.** `SoftwareBuild.status` has five
  states; actions exist for `CLEARED_FOR_FLIGHT` and `REJECTED`, and a build record
  creates one at `BUILT`. Nothing advances a build when a simulation or bench run passes,
  so those two states are reachable only by writing them directly, which is the thing
  these connectors avoid. That transition is the orchestrated build gate in
  `docs/architecture.md` §9, and it needs an action in the pack before a connector can
  drive it.
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
