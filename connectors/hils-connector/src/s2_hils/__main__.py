"""Submit a bench report to S2.

uv run --project connectors/hils-connector hils-submit REPORT.json
uv run --project connectors/hils-connector hils-submit REPORT.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys

from s2_hils.client import DEV_API, DEV_CLIENT_ID, DEV_TOKEN_URL, S2Client, S2Error
from s2_hils.report import ReportError, load
from s2_hils.submit import plan, submit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", help="path to a bench report (s2.bench-report/v1)")
    parser.add_argument("--api", default=DEV_API, help="S2 ontology engine base URL")
    parser.add_argument("--token-url", default=DEV_TOKEN_URL, help="OIDC token endpoint")
    parser.add_argument("--client-id", default=DEV_CLIENT_ID)
    parser.add_argument("--dry-run", action="store_true", help="print the requests without sending them")
    args = parser.parse_args(argv)

    try:
        report = load(args.report)
    except ReportError as exc:
        print(f"{args.report}: off contract: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for call in plan(report):
            print(f"[{call.user}] {call.method} {call.path} {json.dumps(call.body)}")
        return 0

    try:
        with S2Client(api=args.api, token_url=args.token_url, client_id=args.client_id) as client:
            submit(client, report)
    except S2Error as exc:
        print(exc, file=sys.stderr)
        return 1

    run = report.run
    print(
        f"\nSubmitted {run.testRunId}: verdict {run.verdict}, {len(report.anomalies)} "
        f"anomal{'y' if len(report.anomalies) == 1 else 'ies'}.\n"
        f"Try:  GET /v1/objects/TestRun/{run.testRunId}/links/anomalies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
