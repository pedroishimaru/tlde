"""Prebuilt-binary discovery + per-binary metadata.

Layout (no building required — drop already-built artifacts here)::

    target_binaries/<board>/<name>/
        zephyr.elf          # or .hex / .bin
        meta.toml

``meta.toml`` describes how to load + judge the binary; see :class:`BinaryMeta`.
Target-agnostic: nothing is assumed about the board beyond what meta.toml states.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class SuccessCriteria(BaseModel):
    tier: int = 1                       # 1 = boot + no fault + no spin
    virtual_time_budget_s: int = 30
    expect_console: list[str] = Field(default_factory=list)  # tier-2 golden markers
    expect_exit: str | None = None


class BinaryMeta(BaseModel):
    name: str                           # directory name
    dir: str                            # absolute dir holding the artifact
    board: str | None = None
    soc: str | None = None
    file: str = ""                      # artifact filename
    format: str = "elf"                 # elf | hex | bin
    load_address: int | None = None     # required for raw bin
    console_uart: str | None = None     # repl peripheral name for the console
    mcuboot: bool = False
    partitions: dict = Field(default_factory=dict)
    success: SuccessCriteria = Field(default_factory=SuccessCriteria)

    @property
    def artifact_path(self) -> str:
        return str(Path(self.dir) / self.file)


def _coerce_addr(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(str(v), 0)  # handles "0x..." / decimal strings


def load_meta(meta_path: str | Path) -> BinaryMeta:
    meta_path = Path(meta_path)
    data = tomllib.loads(meta_path.read_text())
    b = data.get("binary", {})
    s = data.get("success", {})
    return BinaryMeta(
        name=meta_path.parent.name,
        dir=str(meta_path.parent),
        board=b.get("board"),
        soc=b.get("soc"),
        file=b.get("file", ""),
        format=(b.get("format") or "elf").lower(),
        load_address=_coerce_addr(b.get("load_address")),
        console_uart=b.get("console_uart"),
        mcuboot=bool(b.get("mcuboot", False)),
        partitions=data.get("partitions", {}),
        success=SuccessCriteria(
            tier=int(s.get("tier", 1)),
            virtual_time_budget_s=int(s.get("virtual_time_budget_s", 30)),
            expect_console=list(s.get("expect_console", []) or []),
            expect_exit=s.get("expect_exit"),
        ),
    )


def discover(binaries_dir: str | Path, board: str | None = None) -> list[BinaryMeta]:
    """Find all binaries for ``board`` (or every board) under ``binaries_dir``."""
    root = Path(binaries_dir)
    if not root.is_dir():
        return []
    boards = [root / board] if board else [d for d in root.iterdir() if d.is_dir()]
    metas: list[BinaryMeta] = []
    for bdir in boards:
        if not bdir.is_dir():
            continue
        for meta_file in sorted(bdir.glob("*/meta.toml")):
            try:
                metas.append(load_meta(meta_file))
            except Exception:
                continue
    return metas
