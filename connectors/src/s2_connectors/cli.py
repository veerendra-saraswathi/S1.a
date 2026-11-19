"""Command line entry points.

    hils-submit   a bench report  (s2.bench-report/v1)
    build-submit  a build record  (s2.build-record/v1)

Both take one document, validate it, and either print the calls it becomes or send them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from s2_connectors import bench, build
from s2_connectors.client import DEV_API, DEV_CLIENT_ID, DEV_TOKEN_URL, S2Client, S2Error
from s2_connectors.contract import ContractError
from s2_connectors.model import Call
from s2_connectors.submit import submit


def _cli(
    argv: list[str] | None,
    prog: str,
    what: str,
    load: Callable[[str], Any],
    plan: Callable[[Any], list[Call]],
    summarise: Callable[[Any], str],
) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=f"Write a {what} into S2.")
    parser.add_argument("document", help=f"path to a {what}")
    parser.add_argument("--api", default=DEV_API, help="S2 ontology engine base URL")
    parser.add_argument("--token-url", default=DEV_TOKEN_URL, help="OIDC token endpoint")
    parser.add_argument("--client-id", default=DEV_CLIENT_ID)
    parser.add_argument("--dry-run", action="store_true", help="print the calls without sending them")
    args = parser.parse_args(argv)

    try:
        document = load(args.document)
    except ContractError as exc:
        print(f"{args.document}: off contract: {exc}", file=sys.stderr)
        return 2

    calls = plan(document)

    if args.dry_run:
        for call in calls:
            print(f"[{call.user}] {call.method} {call.path} {json.dumps(call.body)}")
        return 0

    try:
        with S2Client(api=args.api, token_url=args.token_url, client_id=args.client_id) as client:
            submit(client, calls)
    except S2Error as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"\n{summarise(document)}")
    return 0


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def hils(argv: list[str] | None = None) -> int:
    def summarise(report: bench.BenchReport) -> str:
        run = report.run
        return (
            f"Submitted {run.testRunId}: verdict {run.verdict}, "
            f"{_plural(len(report.anomalies), 'anomaly', 'anomalies')}.\n"
            f"Try:  GET /v1/objects/TestRun/{run.testRunId}/links/anomalies"
        )

    return _cli(argv, "hils-submit", "bench report", bench.load, bench.plan, summarise)


def build_(argv: list[str] | None = None) -> int:
    def summarise(record: build.BuildRecord) -> str:
        return (
            f"Submitted {record.build.buildId}: "
            f"{_plural(len(record.build.implements), 'requirement', 'requirements')} implemented, "
            f"{_plural(len(record.testRuns), 'test run', 'test runs')} recorded.\n"
            f"Try:  GET /v1/objects/SoftwareBuild/{record.build.buildId}/links/testRuns"
        )

    return _cli(argv, "build-submit", "build record", build.load, build.plan, summarise)
