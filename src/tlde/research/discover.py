"""Candidate discovery via a pluggable search backend.

A ``SearchBackend`` is any object with an async ``search(query, allowed_domains,
max_results)`` returning :class:`Candidate`s. tlde ships none by default (so a
normal run is offline-safe); users plug one that calls their preferred search
API. Queries are built generically from the target's soc/board strings + the
missing source types — no vendor is hard-coded.
"""

from __future__ import annotations

from typing import Protocol

from tlde.research.policy import ResearchPolicy
from tlde.research.types import Candidate

# Human-readable search terms per structured source type (data, not per-vendor code).
_TYPE_TERMS = {
    "svd": "CMSIS SVD file",
    "dts": "Zephyr devicetree dts",
    "header": "CMSIS device header",
    "pdf": "reference manual datasheet",
}


class SearchBackend(Protocol):
    async def search(self, query: str, allowed_domains: list[str],
                     max_results: int) -> list[Candidate]:
        ...


def build_queries(target, missing_types: list[str]) -> list[str]:
    soc = getattr(target, "soc", None) or ""
    board = getattr(target, "board", None) or ""
    base = soc or board
    if not base:
        return []
    queries: list[str] = []
    for t in missing_types:
        queries.append(f"{base} {_TYPE_TERMS.get(t, t)}")
    queries.append(f"{base} Renode peripheral model")
    if board:
        queries.append(f"{board} Zephyr board devicetree")
    # de-dup preserving order
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


async def discover(policy: ResearchPolicy, target, missing_types: list[str],
                   backend: SearchBackend | None) -> list[Candidate]:
    if backend is None:
        return []
    found: list[Candidate] = []
    for q in build_queries(target, missing_types):
        try:
            found.extend(await backend.search(q, policy.allowlist, policy.max_sources))
        except Exception:
            continue
    return found
