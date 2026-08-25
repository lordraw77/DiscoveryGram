"""Verify NoteDiscovery behaviours that could not be settled from source.

Phase 0, item 6. Two behaviours drive design decisions and can only be
confirmed against a live instance:

  1. Does `POST /api/notes/{path}` overwrite an existing note, or reject it?
     The edit flow is a read-modify-write over POST, so a rejection would force
     a delete-then-create fallback.
  2. Is search enabled, and what is its minimum query length?
     Both are read from `GET /api/config`; search can be disabled server-side.

Run:  uv run python scripts/verify_contract.py
It reads NOTEDISCOVERY_URL and NOTEDISCOVERY_API_KEY from the environment/.env.

The script writes and deletes one scratch note under a dedicated path. It
refuses to touch anything else.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from urllib.parse import quote

import httpx

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
    search = config.get("search", {})
    print(f"    search.enabled              = {search.get('enabled')}")
    print(f"    search.min_query_length     = {search.get('min_query_length', '(absent)')}")
    print(f"    full search block           = {search}")

    if search.get("enabled") is False:
        print("    => /search must be disabled in the bot with an explicit message")


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

    print("\nRecord the results in docs/notediscovery-contract.md section 4.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
