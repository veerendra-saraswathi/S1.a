# Contracts S1.a depends on

## Telemetry contract v1

[`telemetry/v1/`](telemetry/v1/README.md) is a pinned copy of S2's frozen telemetry
contract, `contracts/telemetry/v1/` in the S2 repository: what every S1.a gateway publishes.
It is copied byte for byte and never edited here. `make check-contract` compares it with an
S2 checkout (`S2_DIR`, default `../S2`), and the pipeline workflow does the same against
S2's working branch on every push.

Two sets of tests hold the gateways to it:

| Test | What it checks |
|---|---|
| `gateways/internal/s2/contract_test.go` | The Go types encode the schema's fields, metric names use its pattern, `Timestamp` and `Topic` write its forms, and the topic cases get its verdicts. Standard library only |
| `connectors/tests/test_contract.py` | Runs the gateways' Go tests with `S2_CONTRACT_OUT` set, so each writes every message it would publish (`gateways/internal/s2/s2test`), and validates each topic and payload against the full JSON Schema |

To take a change S2 makes, which for v1 can only be a fix to a case or a schema that
disagreed with the README: copy the directory again, run `make test`, and commit both
together. Anything else is v2, a new directory beside this one.
