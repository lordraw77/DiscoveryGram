"""The metrics registry.

A metrics endpoint is scraped by a machine, so the interesting failures are the
ones a human would never notice reading the output: an unescaped label that
makes the whole page unparseable, a histogram whose buckets are not cumulative,
a label set that grows without bound.
"""

from __future__ import annotations

import pytest

from discoverygram.util.metrics import CONTENT_TYPE, Registry


@pytest.fixture
def registry() -> Registry:
    return Registry()


def test_a_counter_renders_help_type_and_value(registry: Registry) -> None:
    counter = registry.counter("dg_things_total", "Things that happened.")
    counter.inc()
    counter.inc(2)

    assert registry.render().splitlines() == [
        "# HELP dg_things_total Things that happened.",
        "# TYPE dg_things_total counter",
        "dg_things_total 3",
    ]


def test_a_counter_with_no_observations_still_renders_zero(registry: Registry) -> None:
    """A series that appears only after the first event has no baseline to alert on."""
    registry.counter("dg_errors_total", "Errors.")

    assert "dg_errors_total 0" in registry.render()


def test_labels_separate_series(registry: Registry) -> None:
    counter = registry.counter("dg_calls_total", "Calls.")
    counter.inc(outcome="ok")
    counter.inc(outcome="ok")
    counter.inc(outcome="failed")

    assert counter.value(outcome="ok") == 2
    assert counter.value(outcome="failed") == 1
    body = registry.render()
    assert 'dg_calls_total{outcome="ok"} 2' in body
    assert 'dg_calls_total{outcome="failed"} 1' in body


def test_label_order_does_not_create_a_second_series(registry: Registry) -> None:
    counter = registry.counter("dg_calls_total", "Calls.")
    counter.inc(provider="groq", outcome="ok")
    counter.inc(outcome="ok", provider="groq")

    assert counter.value(provider="groq", outcome="ok") == 2
    assert registry.render().count("dg_calls_total{") == 1


def test_a_label_value_cannot_break_the_exposition(registry: Registry) -> None:
    """One unescaped quote and the entire scrape fails to parse, not just this line."""
    counter = registry.counter("dg_calls_total", "Calls.")
    counter.inc(reason='he said "no"\nand \\left')

    line = next(row for row in registry.render().splitlines() if row.startswith("dg_calls_total{"))
    assert line == 'dg_calls_total{reason="he said \\"no\\"\\nand \\\\left"} 1'


def test_a_gauge_goes_both_ways(registry: Registry) -> None:
    gauge = registry.gauge("dg_open", "Open circuits.")
    gauge.set(1, provider="groq")
    gauge.set(0, provider="groq")

    assert gauge.value(provider="groq") == 0


def test_histogram_buckets_are_cumulative(registry: Registry) -> None:
    histogram = registry.histogram("dg_seconds", "Latency.", buckets=(0.1, 1.0, 10.0))
    for value in (0.05, 0.5, 5.0):
        histogram.observe(value)

    lines = histogram.render()
    assert 'dg_seconds_bucket{le="0.1"} 1' in lines
    assert 'dg_seconds_bucket{le="1"} 2' in lines
    assert 'dg_seconds_bucket{le="10"} 3' in lines
    assert 'dg_seconds_bucket{le="+Inf"} 3' in lines
    assert "dg_seconds_count 3" in lines
    assert "dg_seconds_sum 5.55" in lines


def test_an_observation_beyond_every_bucket_still_counts(registry: Registry) -> None:
    histogram = registry.histogram("dg_seconds", "Latency.", buckets=(0.1,))
    histogram.observe(120.0)

    lines = histogram.render()
    assert 'dg_seconds_bucket{le="0.1"} 0' in lines
    assert 'dg_seconds_bucket{le="+Inf"} 1' in lines
    assert histogram.count() == 1


def test_registering_the_same_name_twice_returns_the_same_instrument(registry: Registry) -> None:
    """Two instruments under one name would each hold half the truth."""
    first = registry.counter("dg_calls_total", "Calls.")
    second = registry.counter("dg_calls_total", "Calls.")
    first.inc()
    second.inc()

    assert first is second
    assert first.value() == 2


def test_registering_a_name_as_another_type_is_a_bug_and_says_so(registry: Registry) -> None:
    registry.counter("dg_calls_total", "Calls.")

    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("dg_calls_total", "Calls.")


def test_the_content_type_is_the_one_prometheus_expects() -> None:
    assert CONTENT_TYPE.startswith("text/plain")
    assert "version=0.0.4" in CONTENT_TYPE


def test_the_exposition_ends_with_a_newline(registry: Registry) -> None:
    """A scrape without the trailing newline is rejected by some parsers."""
    registry.counter("dg_calls_total", "Calls.").inc()

    assert registry.render().endswith("\n")
