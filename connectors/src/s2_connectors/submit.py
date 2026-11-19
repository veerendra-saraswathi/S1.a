"""Sending a plan to an engine."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from s2_connectors.client import S2Client, S2Error
from s2_connectors.model import Call


def submit(client: S2Client, calls: Iterable[Call], log: Callable[[str], None] = print) -> None:
    """Send each call in order, stopping at the first refusal it did not expect."""
    for call in calls:
        response = client.request(
            call.user, call.method, call.path, call.body, call.idempotency_key, call.headers
        )
        if call.tolerate_409 and response.status_code == 409:
            log(f"409 {call.method} {call.path}  unchanged: {response.text[:140]}")
            continue
        if response.status_code >= 400:
            hint = ""
            if response.status_code == 404:
                hint = "\na build, drone, requirement or run this document refers to is not in S2 yet."
            raise S2Error(f"{call.method} {call.path} -> {response.status_code}\n{response.text}{hint}")
        note = "  awaiting approval" if response.status_code == 202 else ""
        if response.headers.get("Idempotent-Replay") == "true":
            note += "  (replay)"
        log(f"{response.status_code} {call.method} {call.path}{note}")
