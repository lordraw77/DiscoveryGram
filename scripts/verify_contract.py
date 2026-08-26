"""Verify NoteDiscovery behaviours that could not be settled from source.

Phase 0, item 6. Two behaviours drive design decisions and can only be
confirmed against a live instance:

  1. Does `POST /api/notes/{path}` overwrite an existing note, or reject it?
     The edit flow is a read-modify-write over POST, so a rejection would force
     a delete-then-create fallback.
  2. Is search enabled? `GET /api/config` reports it as the flat, camelCase key
     `searchEnabled`. The minimum query length is *not* reported by any endpoint
     (it is a hard-coded server constant, 2 in 0.31.3), so this script measures
     it instead: it searches for a marker it just wrote, at growing prefix
     lengths, and reports the shortest one that returns a hit.

Run:  uv run python scripts/verify_contract.py
It reads NOTEDISCOVERY_URL and NOTEDISCOVERY_API_KEY from the environment/.env.

The script writes and deletes one scratch note under a dedicated path. It
refuses to touch anything else.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from discoverygram.app.probe import KNOWN_GOOD_VERSION
from discoverygram.config import Settings

SCRATCH_PATH = "DiscoveryGram/_contract_probe.md"
FIRST_BODY = "probe-v1"
SECOND_BODY = "probe-v2"


def _encode(path: str) -> str:
    return quote(path, safe="")


async def _probe_config(client: httpx.AsyncClient) -> None:
    print("\n[2] Search configuration — GET /api/config")
    try:
        response = await client.get("/api/config")
    except httpx.HTTPError as exc:
        print(f"    UNREACHABLE: {exc}")
        return

    if response.status_code != 200:
        print(f"    HTTP {response.status_code} — cannot read config")
        return

    config: dict[str, Any] = response.json()
    enabled = config.get("searchEnabled")
    print(f"    name / version              = {config.get('name')} {config.get('version')}")
    print(f"    searchEnabled               = {enabled}")
    print(f"    authentication.enabled      = {config.get('authentication', {}).get('enabled')}")

    if enabled is False:
        print("    => /search must be disabled in the bot with an explicit message")
    if config.get("version") != KNOWN_GOOD_VERSION:
        print(f"    => version differs from the documented contract ({KNOWN_GOOD_VERSION});")
        print("       re-verify docs/notediscovery-contract.md against this instance")


async def _probe_min_query_length(client: httpx.AsyncClient) -> None:
    """Measure the floor, since no endpoint reports it."""
    print("\n[3] Minimum query length — measured, not reported")
    encoded = _encode(SCRATCH_PATH)
    marker = "zq" + uuid.uuid4().hex[:8]

    create = await client.post(f"/api/notes/{encoded}", json={"content": f"marker {marker}"})
    if create.status_code >= 400:
        print(f"    cannot write the probe note: HTTP {create.status_code}")
        return

    try:
        for length in range(1, len(marker) + 1):
            response = await client.get("/api/search", params={"q": marker[:length], "limit": 50})
            if response.status_code == 403:
                print("    search is disabled (HTTP 403); nothing to measure")
                return
            if response.status_code != 200:
                print(f"    HTTP {response.status_code} at length {length}; stopping")
                return
            if response.json().get("results"):
                print(f"    shortest query that returns a hit = {length}")
                print(f"    => set SEARCH_MIN_QUERY_LENGTH={length}")
                return
        print("    no prefix matched; the marker may not be indexed yet")
    finally:
        await client.delete(f"/api/notes/{encoded}")


async def _probe_overwrite(client: httpx.AsyncClient) -> None:
    print(f"\n[1] POST overwrite semantics — scratch note {SCRATCH_PATH}")
    encoded = _encode(SCRATCH_PATH)

    create = await client.post(f"/api/notes/{encoded}", json={"content": FIRST_BODY})
    print(f"    create        -> HTTP {create.status_code}")
    if create.status_code >= 400:
        print(f"    cannot create scratch note: {create.text[:200]}")
        return

    try:
        overwrite = await client.post(f"/api/notes/{encoded}", json={"content": SECOND_BODY})
        print(f"    re-POST       -> HTTP {overwrite.status_code}")

        read = await client.get(f"/api/notes/{encoded}")
        body = read.text if read.status_code == 200 else ""

        if overwrite.status_code < 400 and SECOND_BODY in body:
            print("    => POST OVERWRITES. Edit = read-modify-write over POST. Plan holds.")
        elif overwrite.status_code >= 400:
            print(f"    => POST REJECTS existing paths (HTTP {overwrite.status_code}).")
            print("       Edit must fall back to delete-then-create.")
        else:
            print("    => POST accepted but content did not change. Inspect manually.")
    finally:
        deleted = await client.delete(f"/api/notes/{encoded}")
        print(f"    cleanup       -> HTTP {deleted.status_code}")


async def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    base_url = str(settings.notediscovery_url).rstrip("/")
    print(f"Probing NoteDiscovery at {base_url}")
    print(f"Authentication: {'X-API-Key' if settings.notediscovery_api_key else 'none'}")

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=settings.notediscovery_headers,
        timeout=settings.notediscovery_timeout,
        verify=settings.notediscovery_verify_tls,
    ) as client:
        try:
            health = await client.get("/health")
        except httpx.HTTPError as exc:
            print(f"FATAL: instance unreachable — {exc}")
            return 1

        print(f"/health -> HTTP {health.status_code}")
        if health.status_code != 200:
            print("FATAL: instance is not healthy")
            return 1

        await _probe_overwrite(client)
        await _probe_config(client)
        await _probe_min_query_length(client)

    print("\nRecord the results in docs/notediscovery-contract.md section 4.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
