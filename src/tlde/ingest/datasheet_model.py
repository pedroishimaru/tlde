"""The structured, page-anchored "datasheet model".

This is the canonical, source-grounded representation that Phase 0 produces and
caches, and that every downstream agent consumes (via the tlde-kb MCP) instead
of re-reading raw PDFs. Every fact carries one or more :class:`Citation`s and a
trust tier, so the verifier can weight by source and so we can *prove* a value
came from a document rather than the model's priors.

Design rules:
  * Numeric fields (offsets, addresses, sizes, reset values) are real ints, not
    hex strings — parsers normalise once, consumers never re-parse.
  * Anything without a citation is, by definition, ungrounded: ``grounded`` is
    False and coverage gates / the verifier treat it as a defect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["svd", "dts", "header", "renode", "pdf", "vision", "web"]


class Citation(BaseModel):
    """Where a fact came from. Page-anchored when the source is a document."""

    source: str                       # file path or URL
    source_type: SourceType
    trust_tier: int = 5               # 1 (vendor/SVD) … 5 (community); lower = more trusted
    page: int | None = None
    section: str | None = None
    table: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # vision provenance (x0,y0,x1,y1)
    note: str | None = None


class BitField(BaseModel):
    name: str
    bit_offset: int
    bit_width: int
    access: str | None = None         # e.g. "rw", "ro", "w1c"
    reset: int | None = None
    enum_values: dict[str, int] = Field(default_factory=dict)
    description: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class Register(BaseModel):
    name: str
    offset: int                       # byte offset from the peripheral base
    size_bits: int = 32
    access: str = "rw"
    reset_value: int | None = None
    description: str | None = None
    bit_fields: list[BitField] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return bool(self.citations)


class Peripheral(BaseModel):
    name: str                         # exact token, e.g. "UARTE0"
    group: str | None = None          # e.g. "UARTE" (shared register layout)
    base_address: int
    size: int                         # address-space span in bytes
    irqs: list[int] = Field(default_factory=list)
    registers: list[Register] = Field(default_factory=list)
    renode_model_hint: str | None = None  # matched upstream Renode peripheral, if any
    description: str | None = None
    citations: list[Citation] = Field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """True only if the peripheral's existence/placement is cited."""
        return bool(self.citations)

    @property
    def coverage(self) -> float:
        """Fraction of registers that carry at least one citation (0..1)."""
        if not self.registers:
            return 1.0 if self.grounded else 0.0
        cited = sum(1 for r in self.registers if r.grounded)
        return cited / len(self.registers)


class MemoryRegion(BaseModel):
    name: str
    base_address: int
    size: int
    kind: Literal["flash", "ram", "peripheral", "rom", "other"] = "other"
    citations: list[Citation] = Field(default_factory=list)


class PinConnection(BaseModel):
    """A board-level net wired to a SoC pin (from schematic and/or DTS)."""

    net: str                          # e.g. "ROW1", "BTN_A", "LED0"
    soc_pin: str                      # e.g. "P0.21"
    peripheral: str | None = None     # owning peripheral, if any
    function: str | None = None       # e.g. "gpio-out", "i2c-sda"
    active_low: bool | None = None
    citations: list[Citation] = Field(default_factory=list)


class Target(BaseModel):
    board: str | None = None
    soc: str | None = None
    arch: str | None = None
    cpu_core: str | None = None


class SourceProvenance(BaseModel):
    """One ingested source: what it was, where from, and how much we trust it."""

    source: str                       # path or URL
    source_type: SourceType
    trust_tier: int
    content_hash: str
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    pages: int | None = None
    note: str | None = None


class CoverageReport(BaseModel):
    """Per-source and overall extraction metrics used by the fail-closed gate."""

    peripherals_total: int = 0
    peripherals_grounded: int = 0
    registers_total: int = 0
    registers_grounded: int = 0
    memory_regions: int = 0
    pins: int = 0
    per_source: dict[str, int] = Field(default_factory=dict)  # source -> facts contributed
    warnings: list[str] = Field(default_factory=list)

    @property
    def peripheral_coverage(self) -> float:
        if self.peripherals_total == 0:
            return 0.0
        return self.peripherals_grounded / self.peripherals_total

    @property
    def register_coverage(self) -> float:
        if self.registers_total == 0:
            return 0.0
        return self.registers_grounded / self.registers_total

    @property
    def overall_coverage(self) -> float:
        """Conservative blend: the weaker of peripheral/register grounding."""
        if self.peripherals_total == 0:
            return 0.0
        if self.registers_total == 0:
            return self.peripheral_coverage
        return min(self.peripheral_coverage, self.register_coverage)


class DatasheetModel(BaseModel):
    """The cached, grounded representation of one target's documentation."""

    target: Target = Field(default_factory=Target)
    memory_map: list[MemoryRegion] = Field(default_factory=list)
    peripherals: list[Peripheral] = Field(default_factory=list)
    connectivity: list[PinConnection] = Field(default_factory=list)
    provenance: list[SourceProvenance] = Field(default_factory=list)
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    content_hash: str = ""

    # -- convenience lookups (used by the tlde-kb MCP) --

    def peripheral(self, name: str) -> Peripheral | None:
        key = name.strip().lower()
        for p in self.peripherals:
            if p.name.lower() == key:
                return p
        return None

    def register(self, peripheral: str, register: str) -> Register | None:
        p = self.peripheral(peripheral)
        if p is None:
            return None
        key = register.strip().lower()
        for r in p.registers:
            if r.name.lower() == key:
                return r
        return None

    def recompute_coverage(self) -> CoverageReport:
        """Recompute aggregate coverage from the current peripherals/registers."""
        cov = self.coverage
        cov.peripherals_total = len(self.peripherals)
        cov.peripherals_grounded = sum(1 for p in self.peripherals if p.grounded)
        cov.registers_total = sum(len(p.registers) for p in self.peripherals)
        cov.registers_grounded = sum(
            1 for p in self.peripherals for r in p.registers if r.grounded
        )
        cov.memory_regions = len(self.memory_map)
        cov.pins = len(self.connectivity)
        return cov

    def is_empty(self) -> bool:
        return not (self.peripherals or self.memory_map or self.connectivity)
