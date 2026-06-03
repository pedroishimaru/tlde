"""Content-hash-keyed cache for the built DatasheetModel.

The cache key is derived from the bytes of every input source plus an extractor
version, so the model is rebuilt automatically when an input changes or the
extraction logic is bumped, and reused across runs and work units otherwise.
Target-agnostic: nothing here knows about any specific vendor or board.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tlde.ingest.datasheet_model import DatasheetModel

# Filenames for the "current run" artifacts the tlde-kb MCP server reads.
CURRENT_MODEL = "current_model.json"
CURRENT_CORPUS = "current_corpus.json"

# Bump when extraction logic changes in a way that should invalidate caches.
EXTRACTOR_VERSION = "1"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def combined_key(source_hashes: list[str]) -> str:
    """Stable cache key for a set of source content hashes + extractor version."""
    joined = "|".join(sorted(source_hashes)) + f"|v{EXTRACTOR_VERSION}"
    return hash_text(joined)


class DatasheetCache:
    """Stores/loads DatasheetModel JSON under ``cache_dir/datasheet/<key>.json``."""

    def __init__(self, cache_dir: str | Path = ".tlde_cache"):
        self.root = Path(cache_dir) / "datasheet"

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def load(self, key: str) -> DatasheetModel | None:
        p = self._path(key)
        if not p.is_file():
            return None
        try:
            return DatasheetModel.model_validate_json(p.read_text())
        except Exception:
            return None  # corrupt cache entry — treat as a miss

    def store(self, key: str, model: DatasheetModel) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path(key)
        p.write_text(model.model_dump_json(indent=2))
        return p


def write_current(cache_dir: str | Path, model: DatasheetModel, corpus: list[dict]) -> None:
    """Persist the active run's model + lexical corpus for the tlde-kb MCP server."""
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / CURRENT_MODEL).write_text(model.model_dump_json(indent=2))
    (root / CURRENT_CORPUS).write_text(json.dumps(corpus))


def read_current(cache_dir: str | Path) -> tuple[DatasheetModel | None, list[dict]]:
    """Load the active run's model + corpus (used by the MCP server subprocess)."""
    root = Path(cache_dir)
    model = None
    mp = root / CURRENT_MODEL
    if mp.is_file():
        try:
            model = DatasheetModel.model_validate_json(mp.read_text())
        except Exception:
            model = None
    corpus: list[dict] = []
    cp = root / CURRENT_CORPUS
    if cp.is_file():
        try:
            corpus = json.loads(cp.read_text())
        except Exception:
            corpus = []
    return model, corpus
