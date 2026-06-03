"""Config dict for attaching the local tlde-kb MCP server to an agent.

Kept import-light (no ``mcp``/heavy deps) so agent modules can include it
cheaply. The server subprocess itself lives in :mod:`tlde.ingest.mcp_server`
and is launched with the current interpreter; it reads the active model/corpus
from ``$TLDE_KB_DIR`` (set by the pipeline in Phase 0, inherited by the child).
"""

from __future__ import annotations

import sys

KB_MCP_NAME = "tlde-kb"


def server_config() -> dict:
    """MCP server config: run ``python -m tlde.ingest.mcp_server`` over stdio."""
    return {"command": sys.executable, "args": ["-m", "tlde.ingest.mcp_server"]}


def kb_mcp_servers() -> dict:
    return {KB_MCP_NAME: server_config()}
