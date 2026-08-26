"""Contract tests: recorded NoteDiscovery payloads -> the domain model.

These are the cheap half of the phase 1 contract suite. The expensive half is
`pytest -m live`, which runs the same expectations against a real instance.
"""

from __future__ import annotations

from datetime import UTC, datetime

from discoverygram.adapters.parsing import (
    parse_config,
    parse_graph,
    parse_media_upload,
    parse_note,
    parse_note_listing,
    parse_search_results,
    parse_share,
    parse_stats,
    parse_tag_notes,
    parse_tags,
    parse_template,
    parse_templates,
    parse_timestamp,
)
from tests.fixtures import notediscovery as fx


def test_parse_timestamp_handles_every_shape_notediscovery_emits() -> None:
    assert parse_timestamp("2026-08-20T10:00:00+00:00") == datetime(2026, 8, 20, 10, tzinfo=UTC)
    assert parse_timestamp("2026-08-20T10:00:00Z") == datetime(2026, 8, 20, 10, tzinfo=UTC)
    # A naive string is assumed UTC rather than local, so comparisons stay total.
    assert parse_timestamp("2026-08-20T10:00:00").tzinfo == UTC  # type: ignore[union-attr]
    assert parse_timestamp(None) is None
    assert parse_timestamp("not a date") is None


def test_parse_note_listing_drops_media_records() -> None:
    """`/api/notes` includes images when the server scans with `include_media`."""
    listing = parse_note_listing(fx.NOTES_LISTING)

    assert [note.path for note in listing.notes] == [
        "Projects/Roadmap.md",
        "Projects/Ideas.md",
        "Journal/2026/Daily.md",
        "Welcome.md",
    ]
    assert "attachments" in listing.folders


def test_parse_note_listing_keeps_tags_and_timestamps() -> None:
    roadmap = parse_note_listing(fx.NOTES_LISTING).notes[0]

    assert roadmap.title == "Roadmap"
    assert roadmap.folder == "Projects"
    assert roadmap.tags == ("planning", "docker")
    assert roadmap.modified == datetime(2026, 8, 20, 10, tzinfo=UTC)


def test_parse_note_reads_metadata_and_backlinks() -> None:
    note = parse_note(fx.NOTE)

    assert note.path == "Projects/Roadmap.md"
    assert note.title == "Roadmap"
    assert note.lines == 3
    assert note.created == datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert [link.path for link in note.backlinks] == ["Projects/Ideas.md"]
    assert note.backlinks[0].references[0].line_number == 4


def test_parse_search_results_cleans_the_html_highlighting() -> None:
    """Raw `<mark>` markup would collide with Telegram's own formatting."""
    hits = parse_search_results(fx.SEARCH_RESULTS)

    assert [hit.path for hit in hits] == ["Projects/Ideas.md", "Projects/docker-notes.md"]
    assert hits[0].matches[0].snippet == "a note about docker & k8s"
    assert hits[0].matches[0].matched_text == "docker"
    assert "<mark" not in hits[1].matches[0].snippet


def test_parse_tags_returns_the_count_map() -> None:
    assert parse_tags(fx.TAGS) == {"planning": 2, "docker": 1}


def test_parse_tag_notes() -> None:
    refs = parse_tag_notes(fx.TAG_NOTES)

    assert [ref.path for ref in refs] == ["Projects/Roadmap.md", "Projects/Ideas.md"]


def test_parse_graph_and_neighbours() -> None:
    graph = parse_graph(fx.GRAPH)

    assert len(graph.nodes) == 3
    # Adjacency ignores direction: "related" means linked either way.
    assert graph.neighbours("Projects/Ideas.md") == ("Projects/Roadmap.md", "Welcome.md")


def test_parse_templates_and_template() -> None:
    templates = parse_templates(fx.TEMPLATES)

    assert [template.name for template in templates] == ["meeting", "daily"]
    assert parse_template(fx.TEMPLATE).content.startswith("# {{title}}")


def test_parse_stats() -> None:
    stats = parse_stats(fx.STATS)

    assert stats.notes_count == 42
    assert stats.version == "0.31.3"
    assert stats.last_modified == datetime(2026, 8, 25, 9, 30, tzinfo=UTC)


def test_parse_share_and_upload() -> None:
    assert parse_share(fx.SHARE).url.endswith("/share/abc123")
    assert parse_media_upload(fx.UPLOAD).path == "attachments/photo.jpg"


def test_parse_config_reads_the_camelcase_keys() -> None:
    """`/api/config` is flat and camelCase — not the `search.enabled` shape assumed in phase 0."""
    config = parse_config(fx.CONFIG, min_query_length=2)

    assert config.version == "0.31.3"
    assert config.search_enabled is True
    assert config.auth_enabled is True
    assert config.min_query_length == 2


def test_parse_config_detects_search_disabled() -> None:
    assert parse_config(fx.CONFIG_SEARCH_DISABLED, min_query_length=2).search_enabled is False


def test_parsers_survive_a_garbage_payload() -> None:
    """A plugin can replace a result set; the bot must not crash on the shape."""
    assert parse_note_listing({"notes": "nope", "folders": None}).notes == ()
    assert parse_search_results({"results": [{"no": "path"}, "junk"]}) == []
    assert parse_tags({}) == {}
    assert parse_graph({"nodes": None, "edges": None}).nodes == ()
