# hils-connector

Turns one hardware-in-the-loop bench run into the engineering record of that run in
[Samanvaya 2](https://github.com/veerendra-saraswathi/S2).

```
bench run -> bench report (JSON) -> hils-connector -> S2 ontology API
```

A bench writes a report when a run finishes. The connector validates it against a
published contract and writes it into S2: the run and its links as ordinary objects, and
the anomalies and the verdict as governed actions. High-rate signals never pass through
here — they stay in the bench's own logs, and the report carries a `logUri` pointing at
them.

## Use

```bash
make submit-dry                                  # from the repo root: print the calls, send nothing
make submit                                      # send them to a local S2
make submit REPORT=runs/TR-HILS-0061.json        # a report of your own
```

Directly, without make:

```bash
uv run --project connectors/hils-connector hils-submit REPORT.json [--api URL] [--dry-run]
```

The ontology pack must be applied first (`make seed` at the repo root). The connector
needs the pack at **0.2.0 or later**, which is the version that added the anomaly window
to `raiseAnomaly`.

## The contract

[`schema/bench-report-v1.schema.json`](schema/bench-report-v1.schema.json) is the
contract, and [`examples/TR-HILS-0058.json`](examples/TR-HILS-0058.json) is a worked
report. `contract` must read `s2.bench-report/v1`; anything else is refused rather than
guessed at. Every enumeration in the schema mirrors the `drone-engineering` pack, so
changing one there is a breaking change here.

A bench supplies as much as it knows. Only `testRunId`, `name`, `kind`, `startedAt` and
`verdict` are required; a run that references no build, drone or requirement simply
produces no links. An anomaly may name a `subsystemId` and a `rootCause` together, and
then the connector confirms it; supply neither and the anomaly is left `OPEN` for a
person to attribute. One without the other is refused — attributing a fault to a
subsystem without saying why is not an attribution.

## What a report becomes

For the worked example, eight calls:

| # | As | Call |
|---|---|---|
| 1 | `admin` | `PUT /v1/objects/TestRun/TR-HILS-0058` |
| 2 | `admin` | `POST …/links/testsBuild` → `BLD-FC-2026.11.3` |
| 3 | `admin` | `POST …/links/ranOn` → `DRN-0001` |
| 4 | `admin` | `POST …/links/verifies` → `SYS-NAV-012` |
| 5 | `operator` | `POST /v1/actions/raiseAnomaly/apply` → `ANM-0058-01` |
| 6 | `operator` | `POST /v1/actions/attributeAnomaly/apply` → gimbal |
| 7 | `operator` | `POST /v1/actions/raiseAnomaly/apply` → `ANM-0058-02`, left open |
| 8 | `operator` | `POST /v1/actions/recordTestVerdict/apply` → `FAIL` |

Two decisions in that sequence are worth knowing about.

**The run is written as `RUNNING`, never with its verdict**, even though the bench has
already reported one. `status` and `verdict` belong to the `recordTestVerdict` action; a
PUT that set them would put a governed state into an ordinary write, which is the thing
the ontology exists to prevent.

**Anomalies are raised before the verdict is recorded**, so a closed run is never left
with its anomalies still missing, and the verdict note can refer to them.

## Re-submitting a report

Benches re-send. Every action carries an `Idempotency-Key`, so a second submission
replays rather than duplicating, and a `409` on `raiseAnomaly` — S2's answer when the
anomaly already exists and nothing changed — is reported and stepped over rather than
treated as a failure.

One subtlety: the verdict's key includes the verdict itself. A bench that *corrects* a
verdict sends a new request rather than replaying the old one.

## Tests

```bash
make test-connector
```

`plan()` is pure — a report in, the exact call sequence out — so the whole mapping is
tested against a stubbed engine rather than a running one. 28 tests cover contract
validation and the sequence, including the 409, 202 and replay paths.

## Known limits

- **It uses the dev realm's `admin` and `operator` users** over the password grant, as
  the rest of this repo does. A real bench should hold a service account of its own with
  only the roles it needs. Nothing here depends on those two users beyond the names.
- **The schema is read from the source tree**, not from package data, so the connector
  runs from a checkout. It is a published contract and lives where people can read it.
- **A PUT replaces the run object.** Re-submitting a report after the verdict was
  recorded briefly returns the run to `RUNNING` before the action completes it again.
- **One report, one run.** There is no batch mode and no watch-a-directory mode; call it
  once per finished run.
- **The token-refresh retry is duplicated** from `tools/seed-chain/seed.py` rather than
  shared, to keep that script standalone under PEP 723.
