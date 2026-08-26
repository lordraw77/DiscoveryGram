"""Validate .env and print a redacted summary.

Answers "is my configuration correct?" without starting the bot or touching
any network. Secrets are never printed, only whether they are present.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from discoverygram.config import Settings
from discoverygram.llm import KNOWN_PROVIDERS, TaskProfile
from discoverygram.llm.factory import provider_supports_vision

# The same capability table the router uses, so the ladder printed here is the
# ladder that will actually be walked — a text-only provider is shown as
# skipped rather than as a rung that would fail on the first photo.
CAPABILITIES = {name: provider_supports_vision(name) for name in KNOWN_PROVIDERS}


def _mark(present: bool) -> str:
    return "set" if present else "EMPTY"


def main() -> int:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration is INVALID:\n")
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location.upper()}: {error['msg']}")
        print("\nFix the values above in .env, then run `make check-env` again.")
        return 1

    url = str(settings.notediscovery_url)

    print("Configuration is VALID.\n")
    print("Telegram")
    print(f"  bot token            {_mark(bool(settings.telegram_bot_token))}")
    print(f"  allowed user ids     {len(settings.telegram_allowed_user_ids)} configured")
    print(f"  mode                 {settings.telegram_mode.value}")
    print("\nNoteDiscovery")
    print(f"  url                  {url}")
    print(f"  api key              {_mark(bool(settings.notediscovery_api_key))}")
    print(f"  transport            {settings.notediscovery_transport.value}")
    print("\nLLM router")
    print(f"  retries per model    {settings.llm_retries_per_model}")
    print(f"  request timeout      {settings.llm_request_timeout_s}s")
    print(
        f"  circuit breaker      opens after {settings.llm_circuit_failure_threshold} "
        f"failures, cools down {settings.llm_circuit_reset_s}s"
    )
    daily = settings.llm_daily_call_limit_per_user
    print(f"  daily cap per user   {daily if daily else 'disabled'}")
    for task in (TaskProfile.CHAT, TaskProfile.VISION):
        ladder, skipped = settings.attempt_ladder(task, capabilities=CAPABILITIES)
        print(f"\n  {task.value} attempt order:")
        if ladder:
            for position, attempt in enumerate(ladder, start=1):
                print(f"    {position}. {attempt}  (x{settings.llm_retries_per_model})")
        else:
            print("    (nothing usable — every provider was skipped)")
        for reason in skipped:
            print(f"    skipped: {reason}")
    print("\nRuntime")
    print(f"  session backend      {settings.session_backend.value}")
    print(f"  health port          {settings.health_port}")

    warnings: list[str] = []
    if "CHANGE-ME" in url:
        warnings.append("NOTEDISCOVERY_URL still contains the CHANGE-ME placeholder.")
    if "localhost" in url or "127.0.0.1" in url:
        warnings.append(
            "NOTEDISCOVERY_URL points at localhost. Inside a container that means the "
            "container itself — use the host's LAN address or host.docker.internal."
        )
    for task, feature in (
        (TaskProfile.CHAT, "LLM-assisted commands stop working"),
        (TaskProfile.VISION, "image-to-note stops working"),
    ):
        ladder, _ = settings.attempt_ladder(task, capabilities=CAPABILITIES)
        providers = {attempt.provider for attempt in ladder}
        if not ladder:
            warnings.append(f"No usable {task.value} model, so {feature}.")
        elif len(providers) == 1:
            warnings.append(
                f"The {task.value} ladder uses a single provider "
                f"({next(iter(providers))}). Extra models add retries but not "
                f"real failover: if that provider is down, {feature}."
            )

    if warnings:
        print("\nWarnings (not fatal):")
        for warning in warnings:
            print(f"  - {warning}")

    print("\nNext: `make verify-contract` to probe the live NoteDiscovery instance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
