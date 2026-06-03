"""Shared data types for web research."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Map a URL/file extension to a structured source type.
_EXT_TYPE = {
    ".svd": "svd", ".dts": "dts", ".dtsi": "dts", ".dtb": "dts",
    ".h": "header", ".hpp": "header", ".pdf": "pdf",
}


def source_type_for(url: str) -> str:
    ext = Path(url.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    return _EXT_TYPE.get(ext, "text")


@dataclass
class Candidate:
    url: str
    title: str = ""
    snippet: str = ""
    source_type: str = ""
    trust_tier: int = 5
    score: float = 0.0
    accepted: bool = True
    reason: str = ""

    def __post_init__(self):
        if not self.source_type:
            self.source_type = source_type_for(self.url)


@dataclass
class FetchedSource:
    path: str
    url: str
    content_hash: str
    source_type: str
    trust_tier: int
    note: str = ""


@dataclass
class ResearchResult:
    fetched: list[FetchedSource] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.fetched]
