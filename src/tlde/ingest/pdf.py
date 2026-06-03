"""Table-aware PDF loader.

Two responsibilities:
  1. Detect corrupt / zero-page / zero-yield PDFs and raise IngestError — this is
     the fix for the historical fails-open bug where a 0-page PDF silently
     produced an empty knowledge base and agents hallucinated register maps.
  2. Produce page-anchored content (text + extracted tables) that the retrieval
     layer indexes with real page numbers, so citations are verifiable.

The PDF deliberately does NOT fabricate structured peripherals/registers — that
high-precision structure comes from SVD/DTS/headers (and, for figures, vision).
The PDF is for behaviour/semantics and gap-filling via grounded retrieval.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from tlde.ingest.cache import hash_file
from tlde.ingest.datasheet_model import DatasheetModel, SourceProvenance
from tlde.ingest.errors import IngestError
from tlde.progress import track

PDF_TRUST_TIER = 3


class PageTable(BaseModel):
    page: int
    rows: list[list[str | None]]

    def to_markdown(self) -> str:
        if not self.rows:
            return ""
        def fmt(r):
            return "| " + " | ".join((c or "").strip().replace("\n", " ") for c in r) + " |"
        out = [fmt(self.rows[0])]
        out.append("| " + " | ".join("---" for _ in self.rows[0]) + " |")
        out.extend(fmt(r) for r in self.rows[1:])
        return "\n".join(out)


class PageContent(BaseModel):
    page: int                      # 1-based page number
    text: str = ""
    tables: list[PageTable] = Field(default_factory=list)


def extract_pages(path: str | Path) -> list[PageContent]:
    """Extract per-page text + tables. Raises IngestError if corrupt/zero-yield."""
    import pdfplumber

    path = str(path)
    pages: list[PageContent] = []
    try:
        with pdfplumber.open(path) as pdf:
            n = len(pdf.pages)
            if n == 0:
                raise IngestError(f"{path}: PDF has 0 pages (corrupt or placeholder).")
            page_iter = track(enumerate(pdf.pages, start=1), total=n,
                              desc=f"[ingest] extract {Path(path).name}", unit="pg")
            for i, page in page_iter:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                tables: list[PageTable] = []
                try:
                    for tbl in (page.extract_tables() or []):
                        if tbl:
                            tables.append(PageTable(page=i, rows=tbl))
                except Exception:
                    pass
                pages.append(PageContent(page=i, text=text, tables=tables))
    except IngestError:
        raise
    except Exception as e:  # malformed/encrypted/unreadable PDF
        raise IngestError(f"{path}: could not read PDF ({type(e).__name__}: {e}).")

    total_text = sum(len(p.text.strip()) for p in pages)
    total_tables = sum(len(p.tables) for p in pages)
    if total_text == 0 and total_tables == 0:
        raise IngestError(
            f"{path}: PDF yielded no extractable text or tables across {len(pages)} "
            f"pages (scanned/image-only?). Enable vision or provide a text PDF."
        )
    return pages


def fragment_from_pages(path: str | Path, pages: list[PageContent],
                        trust_tier: int = PDF_TRUST_TIER) -> DatasheetModel:
    """Build a provenance-only fragment from already-extracted pages (no re-parse)."""
    path = str(path)
    return DatasheetModel(
        provenance=[SourceProvenance(
            source=path, source_type="pdf", trust_tier=trust_tier,
            content_hash=hash_file(path), pages=len(pages),
            note=f"{len(pages)} pages",
        )]
    )


def load(path: str | Path, trust_tier: int = PDF_TRUST_TIER) -> DatasheetModel:
    """Return a provenance-only fragment (page count); raises if corrupt/zero-yield.

    Page content is consumed separately by the retrieval layer (see build.py).
    """
    path = str(path)
    return fragment_from_pages(path, extract_pages(path), trust_tier)
