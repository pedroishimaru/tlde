"""Structured, grounded document ingestion for tlde.

Phase 0 builds a cached, page-anchored :class:`DatasheetModel` from the highest-
signal sources available (SVD > DTS > vendor header > Renode > PDF/vision),
gated fail-loud so corrupt/zero-yield/low-coverage inputs abort instead of
silently producing an empty knowledge base.
"""

from tlde.ingest.build import BuildResult, build_datasheet_model
from tlde.ingest.datasheet_model import DatasheetModel
from tlde.ingest.errors import IngestError

__all__ = ["BuildResult", "build_datasheet_model", "DatasheetModel", "IngestError"]
