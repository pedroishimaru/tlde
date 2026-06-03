"""Autonomous-in-allowlist web research for missing inputs.

Discovers, ranks, and fetches missing machine-readable inputs (SVD/DTS/headers/
PDF/Renode models) from an explicit trust allowlist, recording provenance + a
trust tier for every artifact. Fully optional and policy-gated:

    mode = "off"        → never runs (fully offline)
    mode = "approval"   → discovered sources need human approval before trust
    mode = "autonomous" → fetch from the allowlist without per-source approval

Offline-safe: with no search backend configured it no-ops with a clear note,
so a normal run never depends on the network unless a backend is plugged in.
"""

from __future__ import annotations

from typing import Callable

from tlde.research.discover import SearchBackend, discover
from tlde.research.fetch import fetch
from tlde.research.policy import ResearchPolicy
from tlde.research.rank import rank
from tlde.research.types import Candidate, FetchedSource, ResearchResult

__all__ = ["gather_inputs", "ResearchResult", "Candidate", "SearchBackend"]


async def gather_inputs(
    cfg,
    missing_types: list[str],
    backend: SearchBackend | None = None,
    approve: Callable[[list[Candidate]], list[Candidate]] | None = None,
) -> ResearchResult:
    """Discover + fetch missing inputs per the research policy.

    ``backend`` is the search implementation (None ⇒ offline no-op). ``approve``
    is called in approval mode with the ranked candidates and returns the subset
    the human approved.
    """
    res = ResearchResult()
    policy = ResearchPolicy.from_settings(cfg)
    if not policy.enabled:
        return res
    if not missing_types:
        res.notes.append("web research: no missing source types; nothing to do.")
        return res
    if not (cfg.target.soc or cfg.target.board):
        res.notes.append("web research: set [target].soc/board to enable discovery.")
        return res
    if backend is None:
        res.notes.append(
            "web research enabled but no search backend configured; skipping "
            "discovery (offline-safe). Plug a SearchBackend to activate."
        )
        return res

    candidates = await discover(policy, cfg.target, missing_types, backend)
    ranked = rank(candidates, policy, cfg.target, missing_types)
    if not ranked:
        res.notes.append("web research: no allowlisted candidates matched the target.")
        return res

    if policy.mode == "approval":
        ranked = (approve(ranked) if approve else [])
        if not ranked:
            res.notes.append("web research: no sources approved; skipping.")
            return res

    for c in ranked:
        path, content_hash, reason = await fetch(c.url, cfg.ingest.cache_dir)
        if path is None:
            res.notes.append(f"web research: skip {c.url} ({reason})")
            continue
        res.fetched.append(FetchedSource(
            path=path, url=c.url, content_hash=content_hash,
            source_type=c.source_type, trust_tier=c.trust_tier, note=reason,
        ))
        res.notes.append(
            f"web research: fetched {c.url} → {path} "
            f"(tier {c.trust_tier}, {c.source_type}, {reason})"
        )
    return res
