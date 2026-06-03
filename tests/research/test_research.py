"""Web-research tests — fully offline (stub search backend + stub HTTP client)."""

from __future__ import annotations

import asyncio

import pytest

from tlde import settings as S
from tlde.research import gather_inputs
from tlde.research.fetch import fetch, looks_like_wall
from tlde.research.policy import ResearchPolicy
from tlde.research.rank import rank
from tlde.research.types import Candidate


def _policy(mode="autonomous"):
    cfg = S.Settings()
    cfg.research.mode = mode
    return ResearchPolicy.from_settings(cfg)


def test_policy_allowlist_and_tiers():
    p = _policy()
    assert p.is_allowed("https://developer.arm.com/foo.svd")
    assert p.is_allowed("https://github.com/zephyrproject-rtos/zephyr/x.dts")
    assert not p.is_allowed("https://random-forum.example.com/post")
    assert p.trust_tier("https://github.com/zephyrproject-rtos/zephyr") == 2  # zephyr
    assert p.trust_tier("https://github.com/renode/renode") == 2             # renode
    assert p.trust_tier("https://nordicsemi.com/x.svd") == 1                 # vendor default


class _Target:
    soc = "ACME_MCU"
    board = "acme_dev_board"


def test_rank_wrong_part_guard_and_allowlist():
    p = _policy()
    cands = [
        Candidate(url="https://developer.arm.com/ACME_MCU.svd", title="ACME_MCU SVD"),
        Candidate(url="https://developer.arm.com/OTHER_CHIP.svd", title="OTHER_CHIP SVD"),
        Candidate(url="https://evil.example.com/ACME_MCU.svd", title="ACME_MCU"),
    ]
    out = rank(cands, p, _Target(), missing_types=["svd"])
    urls = [c.url for c in out]
    assert "https://developer.arm.com/ACME_MCU.svd" in urls       # right part, allowed
    assert "https://developer.arm.com/OTHER_CHIP.svd" not in urls  # wrong-part guard
    assert "https://evil.example.com/ACME_MCU.svd" not in urls     # not allowlisted


def test_rank_dedup_and_ordering():
    p = _policy()
    cands = [
        Candidate(url="https://nordicsemi.com/ACME_MCU.svd", title="ACME_MCU"),       # tier1, acme_mcu.svd
        Candidate(url="https://developer.arm.com/ACME_MCU_v2.svd", title="ACME_MCU"),  # tier2, distinct file
        Candidate(url="https://nordicsemi.com/mirror/ACME_MCU.svd", title="ACME_MCU"), # dup of #1
    ]
    out = rank(cands, p, _Target(), missing_types=["svd"])
    assert out[0].trust_tier == 1  # vendor tier1 ranks first
    assert len(out) == 2           # the mirrored duplicate filename is dropped
    assert {c.url.rsplit("/", 1)[-1] for c in out} == {"ACME_MCU.svd", "ACME_MCU_v2.svd"}


def test_paywall_detection():
    assert looks_like_wall(403, "text/html", b"")
    assert looks_like_wall(200, "text/html", b"<html>Please sign in to continue</html>")
    assert looks_like_wall(200, "application/octet-stream", b"\x00binary") is None


class _Resp:
    def __init__(self, status, ctype, body):
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.content = body


class _Client:
    def __init__(self, resp): self._resp = resp
    async def get(self, url): return self._resp
    async def aclose(self): pass


def test_fetch_caches_and_rejects_wall(tmp_path):
    cache = str(tmp_path)
    # good binary fetch
    path, h, reason = asyncio.run(
        fetch("https://nordicsemi.com/a.svd", cache,
              client=_Client(_Resp(200, "application/xml", b"<device/>"))))
    assert path and reason == "fetched" and h
    # second call is a cache hit (client that would error if used)
    class _Boom:
        async def get(self, u): raise AssertionError("should not fetch on cache hit")
        async def aclose(self): pass
    path2, h2, reason2 = asyncio.run(fetch("https://nordicsemi.com/a.svd", cache, client=_Boom()))
    assert path2 == path and reason2 == "cache hit"
    # paywalled response rejected
    p3, _, r3 = asyncio.run(
        fetch("https://nordicsemi.com/b.pdf", cache,
              client=_Client(_Resp(200, "text/html", b"<html>log in</html>"))))
    assert p3 is None and "login" in r3


class _StubBackend:
    def __init__(self, cands): self._cands = cands
    async def search(self, query, allowed_domains, max_results):
        return list(self._cands)


def _cfg(tmp_path, mode="autonomous"):
    cfg = S.Settings()
    cfg.research.mode = mode
    cfg.target.soc = "ACME_MCU"
    cfg.ingest.cache_dir = str(tmp_path / "cache")
    return cfg


def test_gather_inputs_offline_noop_without_backend(tmp_path):
    cfg = _cfg(tmp_path)
    res = asyncio.run(gather_inputs(cfg, ["svd"], backend=None))
    assert not res.fetched and any("no search backend" in n for n in res.notes)


def test_gather_inputs_off_mode(tmp_path):
    cfg = _cfg(tmp_path, mode="off")
    res = asyncio.run(gather_inputs(cfg, ["svd"], backend=_StubBackend([])))
    assert not res.fetched and not res.notes


def test_gather_inputs_autonomous_with_stub(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    backend = _StubBackend([
        Candidate(url="https://nordicsemi.com/ACME_MCU.svd", title="ACME_MCU SVD"),
        Candidate(url="https://evil.example.com/ACME_MCU.svd", title="ACME_MCU"),
    ])

    async def fake_fetch(url, cache_dir, client=None):
        return (f"/tmp/{url.rsplit('/',1)[-1]}", "deadbeef", "fetched")
    # gather_inputs imported `fetch` into the tlde.research namespace; patch it there.
    import tlde.research as R
    monkeypatch.setattr(R, "fetch", fake_fetch)

    res = asyncio.run(gather_inputs(cfg, ["svd"], backend=backend))
    assert res.paths == ["/tmp/ACME_MCU.svd"]            # allowlisted + right part only
    assert res.fetched[0].trust_tier == 1
