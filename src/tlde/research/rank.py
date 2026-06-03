"""Rank, de-duplicate, and guard discovered candidates.

Filters to allowlisted domains, assigns trust tiers, removes duplicates, and
guards against wrong-part-number / version-mismatch documents (a leading cause
of grounding errors). Ranking is by trust tier first, then part-number match.
"""

from __future__ import annotations

import re

from tlde.research.policy import ResearchPolicy
from tlde.research.types import Candidate

_TOK = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOK.findall((text or "").lower()))


def _part_tokens(target) -> set[str]:
    """Identifying tokens for the target (soc/board), normalised."""
    toks: set[str] = set()
    for v in (getattr(target, "soc", None), getattr(target, "board", None)):
        if v:
            toks |= _tokens(v)
    # also a compact form (e.g. "nrf52833") split already handled by tokenizer
    return {t for t in toks if len(t) >= 3}


def rank(candidates: list[Candidate], policy: ResearchPolicy, target,
         missing_types: list[str] | None = None) -> list[Candidate]:
    """Return accepted candidates, best first; rejected ones carry a reason."""
    part = _part_tokens(target)
    accepted: list[Candidate] = []
    seen: set[tuple] = set()

    for c in candidates:
        if not policy.is_allowed(c.url):
            c.accepted, c.reason = False, "domain not in allowlist"
            continue
        if missing_types and c.source_type not in missing_types and c.source_type != "text":
            c.accepted, c.reason = False, f"source_type {c.source_type} not needed"
            continue

        c.trust_tier = policy.trust_tier(c.url)

        # Wrong-part guard: a concrete doc (svd/dts/header/pdf) must mention a
        # target identifier somewhere in its url/title/snippet.
        hay = _tokens(c.url) | _tokens(c.title) | _tokens(c.snippet)
        part_match = bool(part & hay)
        if part and c.source_type in ("svd", "dts", "header", "pdf") and not part_match:
            c.accepted, c.reason = False, "no target part-number match (wrong-part guard)"
            continue

        key = (c.source_type, c.url.rsplit("/", 1)[-1].lower())
        if key in seen:
            c.accepted, c.reason = False, "duplicate"
            continue
        seen.add(key)

        # score: lower tier (more trusted) is better; part match boosts.
        c.score = (6 - c.trust_tier) + (1.0 if part_match else 0.0)
        accepted.append(c)

    accepted.sort(key=lambda x: x.score, reverse=True)
    return accepted[: policy.max_sources]
