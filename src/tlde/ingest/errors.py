"""Ingestion error type shared across loaders and gates."""


class IngestError(Exception):
    """Raised when a source is corrupt/zero-yield or a coverage gate fails.

    The pipeline surfaces this loudly (fail-closed) instead of silently
    proceeding with an empty knowledge base.
    """
