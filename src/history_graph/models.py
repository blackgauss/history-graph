"""Typed models for curated timeline threads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Refs(BaseModel):
    """External identifiers that let us resolve an entry to source systems."""

    model_config = ConfigDict(extra="forbid")

    doi: str | None = None
    title_search: str | None = None
    patent_number: str | None = None


class ThreadEntry(BaseModel):
    """A single milestone on a historical thread."""

    model_config = ConfigDict(extra="forbid")

    id: str
    date: str
    kind: Literal["paper", "patent", "tech", "business"]
    title: str
    who: str | None = None
    sub: Literal["founded", "acquired", "launched", "expired", "hired", "ipo"] | None = None
    refs: Refs = Field(default_factory=Refs)
    related: list[str] = Field(default_factory=list)
    notes: str | None = None

    @property
    def year(self) -> int:
        return int(self.date[:4])


class ThreadFile(BaseModel):
    """Root schema of a thread YAML file."""

    model_config = ConfigDict(extra="forbid")

    entries: list[ThreadEntry]


def parse_thread(data: dict[str, Any]) -> list[ThreadEntry]:
    return ThreadFile.model_validate(data).entries
