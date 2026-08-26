"""Recorded NoteDiscovery 0.31.3 payloads.

Shapes taken from `backend/main.py` and `backend/utils.py` of
`ghcr.io/gamosoft/notediscovery:latest`, not invented — including the quirks the
adapter has to absorb: camelCase config keys, HTML-marked search snippets, the
`{tag: count}` map, and media records sharing the notes listing.
"""

from __future__ import annotations

from typing import Any

CONFIG: dict[str, Any] = {
    "name": "NoteDiscovery",
    "version": "0.31.3",
    "searchEnabled": True,
    "demoMode": False,
    "alreadyDonated": False,
    "autosaveDelayMs": 1000,
    "defaultTheme": "light",
    "uploadMaxNoteMb": 10,
    "sharePublicOrigin": "",
    "authentication": {"enabled": True},
}

CONFIG_SEARCH_DISABLED: dict[str, Any] = {**CONFIG, "searchEnabled": False}

HEALTH: dict[str, Any] = {"status": "healthy", "app": "NoteDiscovery", "version": "0.31.3"}

NOTES_LISTING: dict[str, Any] = {
    "notes": [
        {
            "name": "Roadmap",
            "path": "Projects/Roadmap.md",
            "folder": "Projects",
            "modified": "2026-08-20T10:00:00+00:00",
            "size": 2048,
            "type": "note",
            "tags": ["planning", "docker"],
        },
        {
            "name": "Ideas",
            "path": "Projects/Ideas.md",
            "folder": "Projects",
            "modified": "2026-08-25T09:30:00+00:00",
            "size": 512,
            "type": "note",
            "tags": ["planning"],
        },
        {
            "name": "Daily",
            "path": "Journal/2026/Daily.md",
            "folder": "Journal/2026",
            "modified": "2026-01-02T08:00:00+00:00",
            "size": 128,
            "type": "note",
            "tags": [],
        },
        {
            "name": "Welcome",
            "path": "Welcome.md",
            "folder": "",
            "modified": "2025-12-31T23:59:00+00:00",
            "size": 64,
            "type": "note",
            "tags": [],
        },
        {
            # Media records ride along on the same endpoint and must be dropped.
            "name": "diagram",
            "path": "attachments/diagram.png",
            "folder": "attachments",
            "modified": "2026-02-01T00:00:00+00:00",
            "size": 9001,
            "type": "image",
            "tags": [],
        },
    ],
    "folders": ["Projects", "Journal", "Journal/2026", "Archive", "attachments"],
}

NOTE: dict[str, Any] = {
    "path": "Projects/Roadmap.md",
    "content": "# Roadmap\n\nShip the docker image.\n",
    "metadata": {
        "created": "2026-08-01T12:00:00+00:00",
        "modified": "2026-08-20T10:00:00+00:00",
        "size": 2048,
        "lines": 3,
    },
    "backlinks": [
        {
            "path": "Projects/Ideas.md",
            "name": "Ideas",
            "references": [
                {"line_number": 4, "context": "see [[Roadmap]] for the plan", "type": "wikilink"}
            ],
        }
    ],
}

SEARCH_RESULTS: dict[str, Any] = {
    "results": [
        {
            "name": "Ideas",
            "path": "Projects/Ideas.md",
            "folder": "Projects",
            "matches": [
                {
                    "line_number": 12,
                    "context": (
                        'a note about <mark class="search-highlight">docker</mark> &amp; k8s'
                    ),
                }
            ],
        },
        {
            "name": "docker-notes",
            "path": "Projects/docker-notes.md",
            "folder": "Projects",
            "matches": [
                {
                    "line_number": 1,
                    "context": '...<mark class="search-highlight">Docker</mark> basics...',
                },
                {
                    "line_number": 30,
                    "context": 'run <mark class="search-highlight">docker</mark> compose',
                },
            ],
        },
    ],
    "query": "docker",
}

TAGS: dict[str, Any] = {"tags": {"planning": 2, "docker": 1}}

TAG_NOTES: dict[str, Any] = {
    "tag": "planning",
    "count": 2,
    "notes": [
        {
            "name": "Roadmap",
            "path": "Projects/Roadmap.md",
            "folder": "Projects",
            "modified": "2026-08-20T10:00:00+00:00",
            "size": 2048,
            "tags": ["planning", "docker"],
        },
        {
            "name": "Ideas",
            "path": "Projects/Ideas.md",
            "folder": "Projects",
            "modified": "2026-08-25T09:30:00+00:00",
            "size": 512,
            "tags": ["planning"],
        },
    ],
}

GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "Projects/Roadmap.md", "label": "Roadmap"},
        {"id": "Projects/Ideas.md", "label": "Ideas"},
        {"id": "Welcome.md", "label": "Welcome"},
    ],
    "edges": [
        {"source": "Projects/Ideas.md", "target": "Projects/Roadmap.md", "type": "wikilink"},
        {"source": "Welcome.md", "target": "Projects/Ideas.md", "type": "markdown"},
    ],
}

TEMPLATES: dict[str, Any] = {
    "templates": [
        {"name": "meeting", "path": "_templates/meeting.md", "modified": "2026-05-05T00:00:00Z"},
        {"name": "daily", "path": "_templates/daily.md", "modified": "2026-05-06T00:00:00Z"},
    ]
}

TEMPLATE: dict[str, Any] = {"name": "meeting", "content": "# {{title}}\n\n## Attendees\n"}

STATS: dict[str, Any] = {
    "notes_count": 42,
    "folders_count": 5,
    "tags_count": 7,
    "templates_count": 2,
    "media_count": 3,
    "total_size_bytes": 123456,
    "last_modified": "2026-08-25T09:30:00+00:00",
    "plugins_enabled": 1,
    "version": "0.31.3",
}

SHARE: dict[str, Any] = {
    "success": True,
    "token": "abc123",
    "url": "http://notediscovery.test:8000/share/abc123",
    "path": "Projects/Roadmap.md",
    "theme": "light",
}

UPLOAD: dict[str, Any] = {
    "success": True,
    "path": "attachments/photo.jpg",
    "filename": "photo.jpg",
    "type": "image",
    "message": "Image uploaded successfully",
}

SAVE_OK: dict[str, Any] = {
    "success": True,
    "path": "Projects/Roadmap.md",
    "message": "Note saved successfully",
    "content": "new body",
}
