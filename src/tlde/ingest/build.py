"""Phase 0 orchestrator: build the cached, gated DatasheetModel.

Dispatches each configured source to its loader, merges by precedence, evaluates
the fail-loud coverage gate, caches by content hash, and (optionally) populates
the retrieval knowledge base with page-anchored chunks. Target-agnostic: source
types are inferred from extensions / explicit config slots, never from a vendor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from tlde.ingest import dts as dts_loader
from tlde.ingest import headers as header_loader
from tlde.ingest import pdf as pdf_loader
from tlde.ingest import svd as svd_loader
from tlde.ingest.cache import hash_file
from tlde.ingest.coverage import GateResult, decide
from tlde.ingest.datasheet_model import DatasheetModel
from tlde.ingest.errors import IngestError
from tlde.ingest.merge import PRECEDENCE_TOKEN_TO_TYPE, merge

# Default precedence if the config omits it.
DEFAULT_PRECEDENCE = ["svd", "dts", "vendor_header", "renode_upstream", "reference_manual"]


@dataclass
class TypedSource:
    path: str
    source_type: str  # svd | dts | header | pdf | text
    is_url: bool = False


@dataclass
class BuildResult:
    model: DatasheetModel
    gate: GateResult
    material_chunks: int = 0
    pages_by_source: dict = field(default_factory=dict)  # source -> list[PageContent]


_EXT_TYPE = {
    ".svd": "svd",
    ".dts": "dts", ".dtsi": "dts", ".dtb": "dts",
    ".h": "header", ".hpp": "header",
    ".pdf": "pdf",
}


def _infer_type(path: str) -> str:
    return _EXT_TYPE.get(Path(path).suffix.lower(), "text")


def collect_sources(settings, extra: list[str] | None = None) -> list[TypedSource]:
    """Build the typed source list from settings.sources + extra (CLI/prompt)."""
    s = settings.sources
    typed: list[TypedSource] = []
    seen: set[str] = set()

    def add(p: str | None, forced: str | None = None):
        if not p or p in seen:
            return
        seen.add(p)
        is_url = p.startswith(("http://", "https://"))
        typed.append(TypedSource(p, forced or _infer_type(p), is_url))

    add(s.svd, "svd")
    add(s.dts, "dts")
    add(s.header, "header")
    add(s.reference_manual, "pdf")
    add(s.schematic, "pdf")
    for e in s.extra:
        add(e)
    for e in (extra or []):
        add(e)
    return typed


def _get_pages(cache_dir: str, path: str) -> list:
    """Extract PDF pages, caching the result per file content hash.

    PDF page extraction is the most expensive step; caching it (separately from
    the structured-model cache) keeps re-runs fast AND keeps the retrieval KB
    populated. Raises IngestError for corrupt/zero-yield PDFs (never cached).
    """
    pc = Path(cache_dir) / "pages" / f"{hash_file(path)}.json"
    if pc.is_file():
        try:
            data = json.loads(pc.read_text())
            return [pdf_loader.PageContent(**d) for d in data]
        except Exception:
            pass  # corrupt cache entry — re-extract
    pages = pdf_loader.extract_pages(path)
    try:
        pc.parent.mkdir(parents=True, exist_ok=True)
        pc.write_text(json.dumps([p.model_dump() for p in pages]))
    except Exception:
        pass  # caching is best-effort
    return pages


def _load_structured(src: TypedSource):
    if src.source_type == "svd":
        return svd_loader.load(src.path)
    if src.source_type == "dts":
        return dts_loader.load(src.path)
    if src.source_type == "header":
        return header_loader.load(src.path)
    if src.source_type == "pdf":
        return pdf_loader.load(src.path)  # provenance-only; raises if corrupt
    return None


def _precedence_rank(precedence: list[str]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for i, token in enumerate(precedence):
        stype = PRECEDENCE_TOKEN_TO_TYPE.get(token, token)
        ranks.setdefault(stype, i)
    # types not named in precedence sort after named ones, stable by name
    return ranks


def build_datasheet_model(
    settings,
    extra_sources: list[str] | None = None,
    kb=None,
) -> BuildResult:
    """Build (or load from cache) the DatasheetModel and evaluate the gate."""
    strictness = settings.ingest.strictness
    min_coverage = settings.ingest.min_coverage
    cache_dir = settings.ingest.cache_dir

    typed = collect_sources(settings, extra_sources)

    fragments: list[tuple[int, DatasheetModel]] = []
    pages_by_source: dict[str, list] = {}
    material_chunks = 0
    ranks = _precedence_rank(settings.sources.precedence or DEFAULT_PRECEDENCE)

    # The source loop always runs (PDF extraction is cached per file, structured
    # parsers are fast), so the retrieval KB is always populated — even when the
    # expensive work is a cache hit.
    for t in typed:
        if t.is_url:
            continue  # URLs feed retrieval, not structured loaders (Phase B)
        if not Path(t.path).is_file():
            msg = f"configured source not found: {t.path}"
            if strictness == "fail_closed":
                raise IngestError(msg)
            continue
        try:
            if t.source_type == "pdf":
                pages = _get_pages(cache_dir, t.path)  # cached per file content hash
                pages_by_source[t.path] = pages
                material_chunks += sum(len(p.tables) for p in pages)
                material_chunks += sum(1 for p in pages if p.text.strip())
                frag = pdf_loader.fragment_from_pages(t.path, pages)
            else:
                frag = _load_structured(t)
        except IngestError:
            if strictness == "fail_closed":
                raise
            continue  # quarantine/warn: skip the bad source
        if frag is not None:
            rank = ranks.get(t.source_type, len(ranks) + 1)
            fragments.append((rank, frag))

    fragments.sort(key=lambda rf: rf[0])  # highest precedence first
    model = merge([f for _, f in fragments]) if fragments else DatasheetModel()
    material_chunks += sum(len(p.registers) for p in model.peripherals)

    gate = decide(model, material_chunks, strictness, min_coverage)

    # Populate retrieval KB (if provided and it implements the structured API).
    if kb is not None and gate.ok:
        if hasattr(kb, "ingest_structured"):
            kb.ingest_structured(model)
        if hasattr(kb, "ingest_pages"):
            for src, pages in pages_by_source.items():
                kb.ingest_pages(pages, source=src, source_type="pdf",
                                trust_tier=pdf_loader.PDF_TRUST_TIER)

    return BuildResult(model=model, gate=gate, material_chunks=material_chunks,
                       pages_by_source=pages_by_source)
