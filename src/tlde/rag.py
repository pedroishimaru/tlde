"""Retrieval knowledge base for tlde.

Hybrid retrieval over page-anchored chunks:
  * dense  — sentence-transformers embeddings in ChromaDB (semantic recall)
  * lexical — BM25 over identifier-aware tokens (exact hits like ``UARTE0`` or
    ``0x40002000`` that a small dense embedder misses)
  * rerank — optional cross-encoder over the union (best precision)

Every chunk carries page/section/peripheral/source/trust-tier metadata so the
context block can emit verifiable citations rather than fabricated ones. The
index persists under the ingest cache dir so it is reused across runs.

All heavy models (embedder, reranker) degrade gracefully: if they cannot be
loaded (e.g. offline), retrieval falls back to BM25-only so the pipeline still
runs.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

import chromadb
import httpx

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

_TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|[a-zA-Z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _clean_meta(meta: dict) -> dict:
    """Chroma metadata must be scalar and non-null."""
    return {k: v for k, v in meta.items() if v is not None and isinstance(v, (str, int, float, bool))}


class KnowledgeBase:
    """Hybrid (dense + BM25 + optional rerank) page-anchored retrieval store."""

    def __init__(
        self,
        persist_dir: str | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        reranker_model: str | None = None,
        use_rerank: bool = True,
    ):
        self._embedding_model = embedding_model
        self._reranker_model = reranker_model
        self._use_rerank = use_rerank and bool(reranker_model)
        self._reranker = None
        self._reranker_tried = False

        self._embedding_fn = self._make_embedding_fn(embedding_model)
        if persist_dir:
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=persist_dir)
        else:
            self._client = chromadb.EphemeralClient()

        kwargs = {"name": "specs", "metadata": {"hnsw:space": "cosine"}}
        if self._embedding_fn is not None:
            kwargs["embedding_function"] = self._embedding_fn
        self._collection = self._client.get_or_create_collection(**kwargs)
        self._dense_ok = self._embedding_fn is not None

        # Parallel store for BM25 + rerank candidate reconstruction.
        self._docs: dict[str, dict] = {}  # id -> {text, meta, tokens}

    # ------------------------------------------------------------------
    # Model loading (graceful)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_embedding_fn(model_name: str):
        try:
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )
            return SentenceTransformerEmbeddingFunction(model_name=model_name)
        except Exception as e:  # offline / model unavailable
            print(f"[RAG] dense embeddings unavailable ({e}); using BM25-only.")
            return None

    def _get_reranker(self):
        if self._reranker is not None or self._reranker_tried:
            return self._reranker
        self._reranker_tried = True
        if not self._use_rerank:
            return None
        try:
            from sentence_transformers import CrossEncoder
            self._reranker = CrossEncoder(self._reranker_model)
        except Exception as e:
            print(f"[RAG] reranker unavailable ({e}); skipping rerank.")
            self._reranker = None
        return self._reranker

    # ------------------------------------------------------------------
    # Ingestion — structured facts + page-anchored document content
    # ------------------------------------------------------------------

    def _add(self, text: str, meta: dict) -> None:
        text = text.strip()
        if not text:
            return
        cid = hashlib.sha256(
            f"{meta.get('source','')}:{meta.get('page','')}:{meta.get('peripheral','')}:{text[:120]}".encode()
        ).hexdigest()[:16]
        if cid in self._docs:
            return
        self._docs[cid] = {"text": text, "meta": meta, "tokens": _tokenize(text)}
        if self._dense_ok:
            try:
                self._collection.upsert(ids=[cid], documents=[text], metadatas=[_clean_meta(meta)])
            except Exception:
                self._dense_ok = False  # fall back to BM25-only

    def ingest_structured(self, model) -> int:
        """Index one retrievable chunk per peripheral (its register table)."""
        added = 0
        for p in model.peripherals:
            cite = p.citations[0] if p.citations else None
            meta = {
                "source": cite.source if cite else "structured",
                "source_type": cite.source_type if cite else "svd",
                "trust_tier": cite.trust_tier if cite else 1,
                "peripheral": p.name,
                "page": cite.page if cite else None,
                "kind": "register_table",
            }
            lines = [
                f"# Peripheral {p.name}"
                + (f" (group {p.group})" if p.group else ""),
                f"base_address = {hex(p.base_address)}  size = {hex(p.size)}"
                + (f"  IRQ = {p.irqs}" if p.irqs else ""),
            ]
            if p.description:
                lines.append(p.description)
            if p.registers:
                lines.append("\n| Register | Offset | Reset | Access |")
                lines.append("| --- | --- | --- | --- |")
                for r in p.registers:
                    reset = hex(r.reset_value) if r.reset_value is not None else "?"
                    lines.append(f"| {r.name} | {hex(r.offset)} | {reset} | {r.access} |")
                    for f in r.bit_fields:
                        bits = (f"[{f.bit_offset + f.bit_width - 1}:{f.bit_offset}]"
                                if f.bit_width > 1 else f"[{f.bit_offset}]")
                        lines.append(f"  - {r.name}.{f.name} {bits} {f.access or ''}")
            self._add("\n".join(lines), meta)
            added += 1
        return added

    def ingest_pages(self, pages, source: str, source_type: str = "pdf",
                     trust_tier: int = 3) -> int:
        """Table-aware ingest of page-anchored content (from pdf.extract_pages)."""
        from tlde.progress import track
        added = 0
        for pc in track(pages, total=len(pages),
                        desc="[ingest] embed pages", unit="pg"):
            for tbl in pc.tables:
                self._add(tbl.to_markdown(), {
                    "source": source, "source_type": source_type,
                    "trust_tier": trust_tier, "page": pc.page, "kind": "table",
                })
                added += 1
            for chunk in self._chunk_text(pc.text):
                self._add(chunk, {
                    "source": source, "source_type": source_type,
                    "trust_tier": trust_tier, "page": pc.page, "kind": "text",
                })
                added += 1
        return added

    async def ingest_source(self, source: str) -> int:
        """Backward-compatible ingest of a URL / PDF / text file into retrieval."""
        if source.startswith(("http://", "https://")):
            return await self._ingest_url(source)
        path = Path(source).expanduser().resolve()
        if not path.exists():
            print(f"[RAG] WARNING: file not found: {path}")
            return 0
        if path.suffix.lower() == ".pdf":
            from tlde.ingest.pdf import extract_pages
            return self.ingest_pages(extract_pages(str(path)), source=str(path))
        text = path.read_text(errors="replace")
        return self._add_text_blob(text, str(path), "text", 3)

    async def _ingest_url(self, url: str) -> int:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            from tlde.ingest.pdf import extract_pages
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(resp.content)
                tmp = Path(f.name)
            try:
                return self.ingest_pages(extract_pages(str(tmp)), source=url)
            finally:
                tmp.unlink(missing_ok=True)
        return self._add_text_blob(self._strip_html(resp.text), url, "web", 4)

    def _add_text_blob(self, text: str, source: str, source_type: str, trust_tier: int) -> int:
        added = 0
        for chunk in self._chunk_text(text):
            self._add(chunk, {"source": source, "source_type": source_type,
                              "trust_tier": trust_tier, "kind": "text"})
            added += 1
        return added

    # ------------------------------------------------------------------
    # Retrieval — hybrid
    # ------------------------------------------------------------------

    def query(self, question: str, n_results: int = 12,
              peripheral: str | None = None, page: int | None = None) -> list[dict]:
        if not self._docs:
            return []
        pool = max(n_results * 4, 20)
        candidates: dict[str, dict] = {}

        # dense
        if self._dense_ok and self._collection.count() > 0:
            try:
                res = self._collection.query(
                    query_texts=[question],
                    n_results=min(pool, self._collection.count()),
                )
                for cid, dist in zip(res["ids"][0], res["distances"][0]):
                    if cid in self._docs:
                        candidates.setdefault(cid, {})["dense"] = 1.0 - float(dist)
            except Exception:
                self._dense_ok = False

        # lexical (BM25)
        for cid, score in self._bm25(question, pool):
            candidates.setdefault(cid, {})["bm25"] = score

        # metadata filter
        def keep(cid: str) -> bool:
            m = self._docs[cid]["meta"]
            if peripheral and str(m.get("peripheral", "")).lower() != peripheral.lower():
                return False
            if page is not None and m.get("page") != page:
                return False
            return True

        ids = [c for c in candidates if keep(c)]
        if not ids:
            ids = [c for c in candidates]  # filter too strict — fall back to unfiltered

        ranked = self._rank(question, ids, candidates)
        out = []
        for cid in ranked[:n_results]:
            d = self._docs[cid]
            out.append({"text": d["text"], "meta": d["meta"]})
        return out

    def _bm25(self, question: str, k: int) -> list[tuple[str, float]]:
        try:
            from rank_bm25 import BM25Okapi
        except Exception:
            return []
        ids = list(self._docs)
        corpus = [self._docs[i]["tokens"] for i in ids]
        if not corpus:
            return []
        qtokens = set(_tokenize(question))
        bm = BM25Okapi(corpus)
        scores = bm.get_scores(list(qtokens))
        # Gate on lexical overlap, not score sign: BM25 IDF can go negative on
        # small corpora, which would otherwise discard valid exact-token hits.
        idx = [i for i in range(len(ids)) if qtokens & set(corpus[i])]
        idx.sort(key=lambda i: scores[i], reverse=True)
        idx = idx[:k]
        if not idx:
            return []
        sel = [scores[i] for i in idx]
        lo, hi = min(sel), max(sel)
        span = (hi - lo) or 1.0
        # Normalise to [0,1] for fusion with dense similarity.
        return [(ids[i], (scores[i] - lo) / span) for i in idx]

    def _rank(self, question: str, ids: list[str], cand: dict) -> list[str]:
        reranker = self._get_reranker()
        if reranker is not None and ids:
            try:
                scores = reranker.predict([(question, self._docs[i]["text"]) for i in ids])
                return [i for i, _ in sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)]
            except Exception:
                pass
        # weighted fusion fallback
        def fused(cid: str) -> float:
            c = cand[cid]
            return 0.5 * c.get("dense", 0.0) + 0.5 * c.get("bm25", 0.0)
        return sorted(ids, key=fused, reverse=True)

    def format_context(self, question: str, n_results: int = 12,
                       peripheral: str | None = None, page: int | None = None) -> str:
        chunks = self.query(question, n_results=n_results, peripheral=peripheral, page=page)
        if not chunks:
            return ""
        parts = ["# Reference Documentation (grounded — cite these)\n"]
        for i, ch in enumerate(chunks, 1):
            parts.append(f"## [{i}] {self._cite(ch['meta'])}\n{ch['text']}\n")
        return "\n".join(parts)

    @staticmethod
    def _cite(meta: dict) -> str:
        bits = [str(meta.get("source", "unknown"))]
        if meta.get("page") is not None:
            bits.append(f"p.{meta['page']}")
        if meta.get("section"):
            bits.append(str(meta["section"]))
        if meta.get("peripheral"):
            bits.append(str(meta["peripheral"]))
        if meta.get("source_type"):
            bits.append(f"({meta['source_type']}, tier {meta.get('trust_tier','?')})")
        return " · ".join(bits)

    @property
    def chunk_count(self) -> int:
        return len(self._docs)

    def export_corpus(self) -> list[dict]:
        """Return indexed chunks as ``[{text, meta}]`` (for the tlde-kb MCP server)."""
        return [{"text": d["text"], "meta": d["meta"]} for d in self._docs.values()]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        sections = re.split(r"\n(?=#{1,3} )", text or "")
        chunks: list[str] = []
        for section in sections:
            if len(section) <= CHUNK_SIZE:
                if section.strip():
                    chunks.append(section.strip())
                continue
            paragraphs = section.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) + 2 > CHUNK_SIZE:
                    if current.strip():
                        chunks.append(current.strip())
                    current = (current[-CHUNK_OVERLAP:] + "\n\n" + para
                               if len(current) > CHUNK_OVERLAP else para)
                else:
                    current = current + "\n\n" + para if current else para
            if current.strip():
                chunks.append(current.strip())
        return chunks

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.I)
        text = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
