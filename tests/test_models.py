"""Tests for thread data models."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from history_graph.models import parse_thread


def _thread_data() -> dict[str, Any]:
    return {
        "entries": [
            {
                "id": "maxwell-1865",
                "date": "1865",
                "kind": "paper",
                "title": "A Dynamical Theory of the Electromagnetic Field",
                "who": "James Clerk Maxwell",
                "refs": {"doi": "10.1098/rstl.1865.0008"},
                "related": [],
            },
            {
                "id": "bell-patent-1876",
                "date": "1876-03-06",
                "kind": "patent",
                "title": "Improvements in Telegraphy",
                "sub": "launched",
                "refs": {"patent_number": "174465"},
            },
        ]
    }


def test_parse_thread_extracts_entries_and_years() -> None:
    entries = parse_thread(_thread_data())

    assert [entry.id for entry in entries] == ["maxwell-1865", "bell-patent-1876"]
    assert entries[0].year == 1865
    assert entries[1].year == 1876
    assert entries[0].refs.doi == "10.1098/rstl.1865.0008"
    assert entries[1].refs.patent_number == "174465"


def test_parse_thread_rejects_unknown_kind() -> None:
    data = _thread_data()
    data["entries"][0]["kind"] = "poem"

    with pytest.raises(ValidationError):
        parse_thread(data)


def test_parse_thread_rejects_unknown_fields() -> None:
    data = _thread_data()
    data["entries"][0]["bogus_field"] = True

    with pytest.raises(ValidationError):
        parse_thread(data)
