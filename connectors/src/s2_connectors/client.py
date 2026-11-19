"""A small S2 client: tokens, retries and idempotent requests."""

from __future__ import annotations

import time
from typing import Any

import httpx

DEV_API = "http://localhost:8000"
DEV_TOKEN_URL = "http://localhost:8080/realms/s2/protocol/openid-connect/token"
DEV_CLIENT_ID = "s2-cli"


class S2Error(RuntimeError):
    """S2 refused a request, or could not be reached."""


class S2Client:
    """Talks to one S2 engine as several users.

    A token is fetched on first use per user and kept for the life of the client, which
    suits a submission measured in seconds. The retry on 5xx and transport errors is the
    one `tools/seed-chain/seed.py` carries, for the same reason: Keycloak answers 500 for
    a while after Postgres restarts under it, and refuses connections until its realm is
    loaded. Both clear on their own; a 4xx does not, and fails at once.

    The dev realm's password grant is what this uses, matching the rest of the repo.
    A bench in production should hold a service account of its own with the narrow roles
    it needs, not the shared `admin` and `operator` users.
    """

    def __init__(
        self,
        api: str = DEV_API,
        token_url: str = DEV_TOKEN_URL,
        client_id: str = DEV_CLIENT_ID,
        password_suffix: str = "-dev",
        timeout: float = 30.0,
        attempts: int = 5,
    ) -> None:
        self._http = httpx.Client(base_url=api, timeout=timeout)
        self._token_url = token_url
        self._client_id = client_id
        self._password_suffix = password_suffix
        self._attempts = attempts
        self._tokens: dict[str, str] = {}

    def __enter__(self) -> S2Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def token(self, user: str) -> str:
        if user in self._tokens:
            return self._tokens[user]
        last = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = httpx.post(
                    self._token_url,
                    data={
                        "grant_type": "password",
                        "client_id": self._client_id,
                        "username": user,
                        "password": f"{user}{self._password_suffix}",
                    },
                    timeout=10,
                )
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    self._tokens[user] = response.json()["access_token"]
                    return self._tokens[user]
                last = f"{response.status_code} {response.text[:200]}"
                if response.status_code < 500:
                    break
            if attempt < self._attempts:
                time.sleep(2 ** (attempt - 1))
        raise S2Error(f"could not get a token for '{user}' from {self._token_url}: {last}")

    def request(
        self,
        user: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token(user)}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            headers.update(extra_headers)
        try:
            return self._http.request(method, path, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise S2Error(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
