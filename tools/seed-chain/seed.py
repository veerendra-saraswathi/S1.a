# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "pyyaml>=6"]
# ///
"""Apply the drone-engineering pack to a running S2 engine and seed one worked example.

The example is a single thread through the traceability chain: a system requirement,
the software requirement derived from it, the build that implements it, a simulation
run that passes, a HILS run that fails, the anomaly found in it and the subsystem it is
traced to. It asks for the requirements document to be baselined, which S2 holds for a
second person's approval, and for the build to be cleared for flight, which S2 refuses:
the build passed simulation and not the bench.

    uv run tools/seed-chain/seed.py              # against the local S2 Compose stack
    uv run tools/seed-chain/seed.py --dry-run    # print the requests, send nothing

It needs S2's core-fleet pack and demo drones (`make seed` in the S2 repo) and uses the
dev realm's users: `admin` defines and writes objects, `operator` requests actions.
It also needs S2's OPA to be serving `policies/s2/actions/build_gate.rego`, which
`make install-policy` copies into an S2 checkout.

Running it again changes nothing it has already done. Objects are created once; after
that only their descriptive properties are refreshed, never the fields an action owns
(a build's status, a run's verdict), so a build is not dragged back through the gate the
way a replacing PUT would drag it. An action whose step is already taken is skipped, the
anomaly is kept, and the approval request is not duplicated.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

PACK = Path(__file__).resolve().parents[2] / "ontologies" / "drone-engineering" / "pack.yaml"
DRONE = "DRN-0001"  # created by S2's seed-demo

# (object type, primary key, properties): the values an object is created with. Fields an
# action owns (see governed_fields) are written only at creation.
OBJECTS: list[tuple[str, str, dict[str, Any]]] = [
    ("EngineeringDocument", "DOC-SYSRS-001", {
        "title": "System requirements, night navigation", "kind": "SYSRS", "revision": "B",
        "status": "IN_REVIEW", "author": "HUMAN", "gitRef": "docs/sysrs/night-nav.md@4f2a9c1",
    }),
    ("EngineeringDocument", "DOC-SRS-001", {
        "title": "Software requirements, localisation service", "kind": "SRS", "revision": "A",
        "status": "IN_REVIEW", "author": "AGENT", "gitRef": "docs/srs/localisation.md@9be03d7",
    }),
    ("Requirement", "SYS-NAV-012", {
        "title": "Position accuracy without GNSS", "level": "SYSTEM", "status": "APPROVED",
        "statement": "The drone shall hold position error at or below 10 m without GNSS, updated at 10 Hz or faster.",
        "verificationMethod": "TEST",
    }),
    ("Requirement", "SW-LOC-031", {
        "title": "Fused position output rate", "level": "SOFTWARE", "status": "APPROVED",
        "statement": "The localisation service shall publish a fused geodetic position at 10 Hz or faster.",
        "verificationMethod": "TEST",
    }),
    # The requirement S1.a's own pipeline verifies (tools/pipeline). DRAFT: written here,
    # not yet approved by anyone.
    ("Requirement", "SW-TLM-001", {
        "title": "Gateway telemetry rate and latency", "level": "SOFTWARE", "status": "DRAFT",
        "statement": "The MAVLink gateway shall carry each vehicle's telemetry into S2 at 10 Hz,"
                     " stamped with the vehicle's time, with no message lost after the broker and"
                     " vehicle sample to stored row within 500 ms at the 95th percentile.",
        "verificationMethod": "TEST",
    }),
    ("SoftwareBuild", "BLD-FC-2026.11.3", {
        "name": "Flight software 2026.11.3", "target": "FLIGHT", "gitCommit": "9be03d7",
        "artifactDigest": "sha256:" + "0" * 64, "builtAt": "2026-11-17T09:30:00Z", "status": "BUILT",
    }),
    ("Subsystem", "SUB-DRN1-GIMBAL", {
        "name": "Stabilised camera gimbal", "kind": "PAYLOAD", "partNumber": "GMB-3X-200",
    }),
    ("Subsystem", "SUB-DRN1-NAV", {
        "name": "Night navigation sensor suite", "kind": "NAVIGATION", "partNumber": "NAV-LWIR-VIO-1",
    }),
    ("TestRun", "TR-SIM-0142", {
        "name": "Powerline corridor, 3 drones, night", "kind": "SIMULATION", "status": "COMPLETED",
        "verdict": "PASS", "bench": "Unity fleet simulator",
        "startedAt": "2026-11-17T11:00:00Z", "endedAt": "2026-11-17T11:42:00Z",
    }),
    ("TestRun", "TR-HILS-0057", {
        "name": "HILS endurance, build 2026.11.3", "kind": "HILS", "status": "RUNNING",
        "bench": "HILS bench 2", "startedAt": "2026-11-18T05:10:00Z",
    }),
]

# (source type, source key, link, target key)
LINKS: list[tuple[str, str, str, str]] = [
    ("Requirement", "SYS-NAV-012", "specifiedIn", "DOC-SYSRS-001"),
    ("Requirement", "SW-LOC-031", "specifiedIn", "DOC-SRS-001"),
    ("Requirement", "SW-LOC-031", "derivedFrom", "SYS-NAV-012"),
    ("SoftwareBuild", "BLD-FC-2026.11.3", "implements", "SW-LOC-031"),
    ("Subsystem", "SUB-DRN1-GIMBAL", "installedOn", DRONE),
    ("Subsystem", "SUB-DRN1-NAV", "installedOn", DRONE),
    ("TestRun", "TR-SIM-0142", "testsBuild", "BLD-FC-2026.11.3"),
    ("TestRun", "TR-SIM-0142", "verifies", "SW-LOC-031"),
    ("TestRun", "TR-HILS-0057", "testsBuild", "BLD-FC-2026.11.3"),
    ("TestRun", "TR-HILS-0057", "verifies", "SYS-NAV-012"),
    ("TestRun", "TR-HILS-0057", "ranOn", DRONE),
]

# Actions that wait for approval, and the parameter naming their subject. The key built
# from these keeps a re-run to one pending request per subject instead of a new one each time.
APPROVAL_SUBJECT = {"baselineDocument": "documentId"}

# Refused by the build gate's policy, on purpose. A refusal creates no request, so this
# one needs no key: there is nothing for a re-run to duplicate.
EXPECTED_REFUSAL = "clearBuildForFlight"

# (action, parameters), requested in order by the operator.
ACTIONS: list[tuple[str, dict[str, Any]]] = [
    # Needs approval, and the one action an agent may propose (agent_access: propose),
    # which is the path M3's generated documents take.
    ("baselineDocument", {
        "documentId": "DOC-SYSRS-001", "revision": "B",
        "note": "Seed example: shows the document gate. Approve it, the review is closed.",
    }),
    ("raiseAnomaly", {
        "anomalyId": "ANM-0057-01", "testRunId": "TR-HILS-0057", "severity": "WARNING",
        "title": "Gimbal pitch response slower than the twin predicts",
        "detector": "VISUAL_TWIN", "faultClass": "INCREASED_DAMPING",
    }),
    ("attributeAnomaly", {
        "anomalyId": "ANM-0057-01", "subsystemId": "SUB-DRN1-GIMBAL",
        "rootCause": "Pitch-axis bearing contamination; damping ratio up about 40 percent against baseline.",
    }),
    ("recordTestVerdict", {
        "testRunId": "TR-HILS-0057", "verdict": "FAIL", "note": "One confirmed gimbal anomaly, see ANM-0057-01.",
    }),
    # The build was written at BUILT; the simulation run that passed earns it one step.
    ("advanceBuild", {
        "buildId": "BLD-FC-2026.11.3", "stage": "SIM_PASSED", "testRunId": "TR-SIM-0142",
        "note": "TR-SIM-0142 passed.",
    }),
    # The HILS run failed, so the build never reached BENCH_PASSED and S2 refuses even
    # the request. The build connector's example, 2026.11.4, is the one that gets through.
    ("clearBuildForFlight", {
        "buildId": "BLD-FC-2026.11.3",
        "justification": "Seed example: shows the build gate refusing a build that failed the bench.",
    }),
]


# The gate's order, for deciding whether a build already stands at or past a step.
GATE = ["BUILT", "SIM_PASSED", "BENCH_PASSED", "CLEARED_FOR_FLIGHT"]

# Sent with If-None-Match: * so an existing object is not replaced; S2 answers 409.
CREATE = "PUT-IF-ABSENT"


def governed_fields(pack: dict[str, Any]) -> dict[str, set[str]]:
    """Per object type, the properties an action of the pack modifies: only actions set them."""
    owned: dict[str, set[str]] = {}
    for action in pack.get("action_types", []):
        for edit in action.get("edits", []):
            if edit.get("op") == "modify_object":
                owned.setdefault(edit["object_type"], set()).update(edit.get("assign", {}))
    return owned


def plan() -> list[tuple[str, str, str, dict[str, Any]]]:
    """Every request as (user, method, path, body), in the order it is sent."""
    pack = yaml.safe_load(PACK.read_text())
    owned = governed_fields(pack)
    steps: list[tuple[str, str, str, dict[str, Any]]] = [("admin", "POST", "/v1/ontology/packs:apply", pack)]
    for kind, key, props in OBJECTS:
        path = f"/v1/objects/{kind}/{key}"
        steps.append(("admin", CREATE, path, {"properties": props}))
        descriptive = {k: v for k, v in props.items() if k not in owned.get(kind, set())}
        if descriptive:
            steps.append(("admin", "PATCH", path, {"properties": descriptive}))
    steps += [
        ("admin", "POST", f"/v1/objects/{kind}/{key}/links/{link}", {"target_primary_key": target})
        for kind, key, link, target in LINKS
    ]
    steps += [("operator", "POST", f"/v1/actions/{name}/apply", {"parameters": params}) for name, params in ACTIONS]
    return steps


def fetch_token(token_url: str, client_id: str, user: str, attempts: int = 5) -> str:
    """Ask Keycloak for a password-grant token, retrying while it is still warming up.

    Keycloak answers 500 unknown_error for a while after Postgres restarts under it,
    and refuses connections until its realm is loaded. Both clear on their own, so a
    5xx or a transport error is retried with backoff. A 4xx is a real answer -- wrong
    realm, client or password -- and fails at once.
    """
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "username": user,
                    "password": f"{user}-dev",
                },
                timeout=10,
            )
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                return response.json()["access_token"]
            last = f"{response.status_code} {response.text[:200]}"
            if response.status_code < 500:
                break
        if attempt < attempts:
            delay = 2 ** (attempt - 1)
            print(f"token for '{user}': {last}; retrying in {delay}s ({attempt}/{attempts - 1})")
            time.sleep(delay)
    sys.exit(f"could not get a token for '{user}' from {token_url}: {last}")


def already_done(client: httpx.Client, headers: dict[str, str], action: str, params: dict[str, Any]) -> str | None:
    """Why an action's step needs no doing on a re-run, or None to send it."""
    def get(path: str) -> dict[str, Any] | None:
        response = client.get(path, headers=headers)
        return response.json()["properties"] if response.status_code == 200 else None

    if action == "advanceBuild":
        build = get(f"/v1/objects/SoftwareBuild/{params['buildId']}")
        status = (build or {}).get("status")
        if status == "REJECTED" or (status in GATE and GATE.index(status) >= GATE.index(params["stage"])):
            return f"{params['buildId']} already stands at {status}"
    if action == "recordTestVerdict":
        run = get(f"/v1/objects/TestRun/{params['testRunId']}")
        if run and run.get("status") == "COMPLETED" and run.get("verdict") == params["verdict"]:
            return f"{params['testRunId']} already holds verdict {params['verdict']}"
    if action == "attributeAnomaly":
        anomaly = get(f"/v1/objects/Anomaly/{params['anomalyId']}")
        if anomaly and anomaly.get("rootCause") == params["rootCause"]:
            return f"{params['anomalyId']} is already attributed"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://localhost:8000", help="S2 ontology engine base URL")
    parser.add_argument(
        "--token-url", default="http://localhost:8080/realms/s2/protocol/openid-connect/token", help="OIDC token endpoint"
    )
    parser.add_argument("--client-id", default="s2-cli")
    parser.add_argument("--dry-run", action="store_true", help="print the requests without sending them")
    args = parser.parse_args()

    steps = plan()
    if args.dry_run:
        for user, method, path, body in steps:
            shown = {"name": body["name"], "version": body["version"]} if path.endswith("packs:apply") else body
            print(f"[{user}] {method} {path} {json.dumps(shown)}")
        return

    tokens: dict[str, str] = {}
    with httpx.Client(base_url=args.api, timeout=30) as client:
        for user, method, path, body in steps:
            if user not in tokens:
                tokens[user] = fetch_token(args.token_url, args.client_id, user)
            headers = {"Authorization": f"Bearer {tokens[user]}"}
            action = path.split("/")[3] if path.startswith("/v1/actions/") else ""
            if action:
                if "admin" not in tokens:
                    tokens["admin"] = fetch_token(args.token_url, args.client_id, "admin")
                reason = already_done(client, {"Authorization": f"Bearer {tokens['admin']}"}, action, body["parameters"])
                if reason:
                    print(f"skip {method} {path}  {reason}")
                    continue
            if method == CREATE:
                response = client.put(path, json=body, headers={**headers, "If-None-Match": "*"})
                if response.status_code == 409:
                    print(f"409 PUT {path}  exists; not replaced")
                    continue
                if response.status_code >= 400:
                    sys.exit(f"PUT {path} -> {response.status_code}\n{response.text}")
                print(f"{response.status_code} PUT {path}  created")
                continue
            if action in APPROVAL_SUBJECT:
                subject = body["parameters"][APPROVAL_SUBJECT[action]]
                headers["Idempotency-Key"] = f"seed-chain:{action}:{subject}"
            response = client.request(method, path, json=body, headers=headers)
            if response.status_code == 409 and "raiseAnomaly" in path:
                # S2 answers 409 when an action fails and changes nothing. On a re-run that is
                # the anomaly already existing; anything else shows in the text printed here.
                print(f"409 {method} {path}  not raised again: {response.text[:160]}")
                continue
            if response.status_code == 403 and action == EXPECTED_REFUSAL:
                reasons = response.json().get("detail", {}).get("reasons") or [response.text[:160]]
                print(f"403 {method} {path}  refused by the build gate: {'; '.join(reasons)}")
                continue
            if response.status_code >= 400:
                hint = ""
                if response.status_code == 403 and action == "advanceBuild":
                    hint = "\nif no reason is given, S2's OPA is not serving build_gate.rego: run `make install-policy`."
                if response.status_code == 404 and DRONE in json.dumps(body):
                    hint = f"\n{DRONE} is missing: run `make seed` in the S2 repo first."
                sys.exit(f"{method} {path} -> {response.status_code}\n{response.text}{hint}")
            note = ""
            if path.endswith("packs:apply"):
                result = response.json()
                note = f"  created {len(result.get('created', []))}, updated {len(result.get('updated', []))}"
            elif response.status_code == 202:
                note = "  awaiting approval"
            print(f"{response.status_code} {method} {path}{note}")

    print(
        "\nSeeded. One request waits in S2's Approvals queue for `approver` to decide:\n"
        "  baselineDocument  DOC-SYSRS-001 rev B   approve it, the review is done\n"
        "BLD-FC-2026.11.3 stands at SIM_PASSED or later, and its flight clearance was refused: the HILS run failed.\n"
        "Try:  GET /v1/objects/Requirement/SYS-NAV-012/links/verifiedBy"
    )


if __name__ == "__main__":
    main()
