"""Prometheus metrics, without a Prometheus client.

The exposition format is a few lines of text, and the whole of what this bot
needs is counters, gauges and histograms with low-cardinality labels. Pulling
in `prometheus_client` for that would add a dependency — and a second global
registry, a second concurrency model and a WSGI server we would then have to
keep out of the event loop — to produce the same forty lines of output.

Two rules keep it honest:

* **Instruments are always live, the endpoint is not.** `METRICS_ENABLED`
  gates `/metrics`, never the recording. An operator turning metrics on gets
  numbers from a running process rather than from zero, and no counter site in
  the codebase has to ask whether it is switched on.
* **Labels are bounded by construction.** Every label value here is a provider
  name, an HTTP method, or an outcome from a fixed set. Nothing that a Telegram
  user can influence — a note path, a query, a user id — is ever a label, because
  that is how a metrics endpoint becomes an out-of-memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Seconds. Sized for what is actually being timed: a NoteDiscovery call is tens
# of milliseconds, an LLM call is seconds to tens of seconds, and both share
# these buckets so one histogram definition serves both.
DEFAULT_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

LabelValues = tuple[tuple[str, str], ...]


def _key(labels: Mapping[str, str] | None) -> LabelValues:
    """Label values as a hashable, order-independent key."""
    if not labels:
        return ()
    return tuple(sorted((name, str(value)) for name, value in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def _render_labels(labels: LabelValues, extra: tuple[str, str] | None = None) -> str:
    pairs = list(labels)
    if extra is not None:
        pairs.append(extra)
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + inner + "}"


def _number(value: float) -> str:
    """Render a value the way Prometheus expects, without float noise."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 6))


@dataclass(slots=True)
class _Instrument:
    name: str
    help_text: str

    def render(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(slots=True)
class Counter(_Instrument):
    """A monotonically increasing count."""

    values: dict[LabelValues, float] = field(default_factory=dict)

    def inc(self, amount: float = 1.0, /, **labels: str) -> None:
        key = _key(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self.values.get(_key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        if not self.values:
            lines.append(f"{self.name} 0")
        lines.extend(
            f"{self.name}{_render_labels(key)} {_number(value)}"
            for key, value in sorted(self.values.items())
        )
        return lines


@dataclass(slots=True)
class Gauge(_Instrument):
    """A value that goes both ways."""

    values: dict[LabelValues, float] = field(default_factory=dict)

    def set(self, value: float, /, **labels: str) -> None:
        self.values[_key(labels)] = float(value)

    def value(self, **labels: str) -> float:
        return self.values.get(_key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        lines.extend(
            f"{self.name}{_render_labels(key)} {_number(value)}"
            for key, value in sorted(self.values.items())
        )
        return lines


@dataclass(slots=True)
class Histogram(_Instrument):
    """Cumulative buckets, a sum and a count — the standard three."""

    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: dict[LabelValues, list[float]] = field(default_factory=dict)
    sums: dict[LabelValues, float] = field(default_factory=dict)
    totals: dict[LabelValues, float] = field(default_factory=dict)

    def observe(self, value: float, /, **labels: str) -> None:
        key = _key(labels)
        counts = self.counts.get(key)
        if counts is None:
            counts = [0.0] * len(self.buckets)
            self.counts[key] = counts
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                counts[index] += 1
        self.sums[key] = self.sums.get(key, 0.0) + value
        self.totals[key] = self.totals.get(key, 0.0) + 1

    def count(self, **labels: str) -> float:
        return self.totals.get(_key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for key in sorted(self.counts):
            counts = self.counts[key]
            for edge, count in zip(self.buckets, counts, strict=True):
                label = _render_labels(key, ("le", _number(edge)))
                lines.append(f"{self.name}_bucket{label} {_number(count)}")
            total = self.totals[key]
            lines.append(
                f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} {_number(total)}"
            )
            lines.append(f"{self.name}_sum{_render_labels(key)} {_number(self.sums[key])}")
            lines.append(f"{self.name}_count{_render_labels(key)} {_number(total)}")
        return lines


class Registry:
    """Every instrument the process owns, and the text they render to."""

    def __init__(self) -> None:
        self._instruments: dict[str, _Instrument] = {}

    def counter(self, name: str, help_text: str) -> Counter:
        return self._register(Counter(name=name, help_text=help_text))

    def gauge(self, name: str, help_text: str) -> Gauge:
        return self._register(Gauge(name=name, help_text=help_text))

    def histogram(
        self, name: str, help_text: str, *, buckets: Sequence[float] = DEFAULT_BUCKETS
    ) -> Histogram:
        return self._register(
            Histogram(name=name, help_text=help_text, buckets=tuple(sorted(buckets)))
        )

    def _register[T: _Instrument](self, instrument: T) -> T:
        """Registering the same name twice returns the first instrument.

        Modules build their instruments at import time, and a test that reloads
        one must not end up with two counters that each hold half the truth.
        """
        existing = self._instruments.get(instrument.name)
        if existing is not None:
            if type(existing) is not type(instrument):
                raise ValueError(f"metric {instrument.name} is already registered as another type")
            return existing
        self._instruments[instrument.name] = instrument
        return instrument

    def instruments(self) -> Iterable[_Instrument]:
        return list(self._instruments.values())

    def render(self) -> str:
        lines: list[str] = []
        for name in sorted(self._instruments):
            lines.extend(self._instruments[name].render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Drop every instrument. Tests only — a live registry never resets."""
        self._instruments.clear()


REGISTRY = Registry()

# --- The instruments -----------------------------------------------------
#
# Named `discoverygram_*`, and grouped by the question each answers:
# is the bot serving? is the vault answering? is the AI layer healthy?

BUILD_INFO = REGISTRY.gauge("discoverygram_build_info", "Build metadata, always 1.")

UPDATES = REGISTRY.counter(
    "discoverygram_updates_total", "Telegram updates seen, by allow-list outcome."
)
HANDLER_ERRORS = REGISTRY.counter(
    "discoverygram_handler_errors_total", "Failures reaching the global error handler, by kind."
)

NOTESTORE_REQUESTS = REGISTRY.counter(
    "discoverygram_notediscovery_requests_total", "NoteDiscovery calls, by method and outcome."
)
NOTESTORE_LATENCY = REGISTRY.histogram(
    "discoverygram_notediscovery_request_seconds", "NoteDiscovery call latency, by method."
)
NOTESTORE_RETRIES = REGISTRY.counter(
    "discoverygram_notediscovery_retries_total", "NoteDiscovery calls retried, by reason."
)
NOTESTORE_THROTTLED = REGISTRY.counter(
    "discoverygram_notediscovery_throttled_seconds_total",
    "Seconds spent waiting on the client-side rate limiter, by bucket.",
)

CACHE_EVENTS = REGISTRY.counter(
    "discoverygram_cache_events_total", "Hot-read cache events, by cache and event."
)

LLM_ATTEMPTS = REGISTRY.counter(
    "discoverygram_llm_attempts_total", "Provider calls, by provider and outcome."
)
LLM_LATENCY = REGISTRY.histogram(
    "discoverygram_llm_latency_seconds", "Provider call latency, by provider."
)
LLM_REQUESTS = REGISTRY.counter(
    "discoverygram_llm_requests_total", "User-visible AI requests, by outcome."
)
LLM_FAILOVERS = REGISTRY.counter(
    "discoverygram_llm_failovers_total", "Requests served by a rung other than the first."
)
LLM_THROTTLED = REGISTRY.counter(
    "discoverygram_llm_throttled_total", "AI requests refused by a local limit, by limit."
)
LLM_CIRCUIT_STATE = REGISTRY.gauge(
    "discoverygram_llm_circuit_open",
    "1 when a provider's circuit is not closed, by provider and state.",
)

TOKENS = REGISTRY.counter(
    "discoverygram_llm_tokens_total", "Tokens accounted, by provider and kind."
)


def render() -> str:
    return REGISTRY.render()


__all__ = [
    "BUILD_INFO",
    "CACHE_EVENTS",
    "CONTENT_TYPE",
    "DEFAULT_BUCKETS",
    "HANDLER_ERRORS",
    "LLM_ATTEMPTS",
    "LLM_CIRCUIT_STATE",
    "LLM_FAILOVERS",
    "LLM_LATENCY",
    "LLM_REQUESTS",
    "LLM_THROTTLED",
    "NOTESTORE_LATENCY",
    "NOTESTORE_REQUESTS",
    "NOTESTORE_RETRIES",
    "NOTESTORE_THROTTLED",
    "REGISTRY",
    "TOKENS",
    "UPDATES",
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "render",
]
