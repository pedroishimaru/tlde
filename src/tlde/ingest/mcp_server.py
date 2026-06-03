"""tlde-kb — agentic retrieval MCP server.

Serves the cached, grounded DatasheetModel to agents as *structured slices with
citations* (a register table for a named peripheral, a memory map, a pin map)
instead of dumping raw PDF text or pushing whole PDFs through per turn. This is
how agents consume only the relevant grounded facts, with verifiable citations,
and how the verifier cross-checks against the source of truth.

Reads the active run's artifacts from ``$TLDE_KB_DIR`` (default ``.tlde_cache``):
  * current_model.json  — the DatasheetModel
  * current_corpus.json — page-anchored chunks for lexical search_docs

Runs over stdio; launched by the Copilot SDK via tlde.ingest.mcp_config.
Lexical-only (BM25) search keeps the subprocess light — no embedding model.
"""

from __future__ import annotations

import os
import re

from mcp.server.fastmcp import FastMCP

from tlde.ingest.cache import read_current
from tlde.ingest.datasheet_model import DatasheetModel

_TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|[a-zA-Z0-9_]+")

mcp = FastMCP("tlde-kb")

_CACHE_DIR = os.environ.get("TLDE_KB_DIR", ".tlde_cache")
_MODEL: DatasheetModel | None = None
_CORPUS: list[dict] = []


def _load() -> None:
    global _MODEL, _CORPUS
    if _MODEL is None:
        _MODEL, _CORPUS = read_current(_CACHE_DIR)
        if _MODEL is None:
            _MODEL = DatasheetModel()


def _cite(c) -> dict:
    return {
        "source": c.source, "source_type": c.source_type, "trust_tier": c.trust_tier,
        "page": c.page, "section": c.section, "table": c.table,
    }


def _register_dict(r) -> dict:
    return {
        "name": r.name, "offset": hex(r.offset), "size_bits": r.size_bits,
        "access": r.access,
        "reset_value": (hex(r.reset_value) if r.reset_value is not None else None),
        "description": r.description,
        "bit_fields": [
            {"name": f.name, "bit_offset": f.bit_offset, "bit_width": f.bit_width,
             "access": f.access, "reset": f.reset, "enum_values": f.enum_values,
             "description": f.description}
            for f in r.bit_fields
        ],
        "citations": [_cite(c) for c in r.citations],
    }


def _peripheral_dict(p, with_registers: bool = True) -> dict:
    d = {
        "name": p.name, "group": p.group, "base_address": hex(p.base_address),
        "size": hex(p.size), "irqs": p.irqs, "renode_model_hint": p.renode_model_hint,
        "description": p.description, "grounded": p.grounded,
        "coverage": round(p.coverage, 3),
        "citations": [_cite(c) for c in p.citations],
    }
    if with_registers:
        d["registers"] = [_register_dict(r) for r in p.registers]
    return d


@mcp.tool()
def list_peripherals() -> dict:
    """List every peripheral with base address, size, IRQs and grounded status."""
    _load()
    return {
        "target": _MODEL.target.model_dump(),
        "peripherals": [_peripheral_dict(p, with_registers=False) for p in _MODEL.peripherals],
    }


@mcp.tool()
def get_peripheral(name: str) -> dict:
    """Get the full grounded register table + citations for one peripheral.

    Use this instead of reading the datasheet PDF directly. Returns an error
    dict (with suggestions) if the peripheral is unknown.
    """
    _load()
    p = _MODEL.peripheral(name)
    if p is None:
        avail = [pp.name for pp in _MODEL.peripherals]
        sugg = [n for n in avail if name.lower() in n.lower() or n.lower() in name.lower()]
        return {"error": f"unknown peripheral {name!r}", "suggestions": sugg or avail[:20]}
    return _peripheral_dict(p)


@mcp.tool()
def get_register(peripheral: str, register: str) -> dict:
    """Get one register's offset, reset value, access and bit-fields, with citations."""
    _load()
    r = _MODEL.register(peripheral, register)
    if r is None:
        p = _MODEL.peripheral(peripheral)
        if p is None:
            return {"error": f"unknown peripheral {peripheral!r}"}
        return {"error": f"unknown register {register!r} in {peripheral}",
                "available": [rr.name for rr in p.registers]}
    return _register_dict(r)


@mcp.tool()
def get_memory_map() -> dict:
    """Return the memory map (flash/ram/peripheral regions) with citations."""
    _load()
    return {"memory_map": [
        {"name": m.name, "kind": m.kind, "base_address": hex(m.base_address),
         "size": hex(m.size), "citations": [_cite(c) for c in m.citations]}
        for m in _MODEL.memory_map
    ]}


@mcp.tool()
def get_pinmap() -> dict:
    """Return board connectivity (LED/button/net -> SoC pin) with citations."""
    _load()
    return {"connectivity": [
        {"net": c.net, "soc_pin": c.soc_pin, "peripheral": c.peripheral,
         "function": c.function, "active_low": c.active_low,
         "citations": [_cite(ci) for ci in c.citations]}
        for c in _MODEL.connectivity
    ]}


@mcp.tool()
def search_docs(query: str, peripheral: str | None = None, page: int | None = None,
                max_results: int = 6) -> dict:
    """Lexical (BM25) search over page-anchored doc chunks. Returns text + citations.

    Use for behaviour/semantics not in the structured register tables. Optionally
    filter by ``peripheral`` or ``page``.
    """
    _load()
    docs = _CORPUS
    if peripheral:
        docs = [d for d in docs if str(d["meta"].get("peripheral", "")).lower() == peripheral.lower()]
    if page is not None:
        docs = [d for d in docs if d["meta"].get("page") == page]
    if not docs:
        return {"results": []}
    try:
        from rank_bm25 import BM25Okapi
        corpus = [_TOKEN_RE.findall(d["text"].lower()) for d in docs]
        qtokens = set(_TOKEN_RE.findall(query.lower()))
        bm = BM25Okapi(corpus)
        scores = bm.get_scores(list(qtokens))
        # Gate on lexical overlap (BM25 IDF goes negative on tiny corpora, so a
        # score>0 filter would wrongly drop exact matches); rank the rest by BM25.
        cand = [i for i in range(len(docs)) if qtokens & set(corpus[i])]
        order = sorted(cand, key=lambda i: scores[i], reverse=True)[:max_results]
    except Exception:
        order = list(range(min(max_results, len(docs))))
    return {"results": [{"text": docs[i]["text"], "citation": docs[i]["meta"]} for i in order]}


def main() -> None:
    _load()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
