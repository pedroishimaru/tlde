"""Merge DatasheetModel fragments by source precedence.

Higher-precedence sources seed the canonical values; lower-precedence sources
fill gaps and add confirmation citations (never overriding a higher source).
This is how SVD (register maps) composes with DTS (addresses/IRQs/wiring),
vendor headers (base addresses), and the PDF/vision (semantics/figures) without
any of them being assumed correct on its own. Target-agnostic throughout.
"""

from __future__ import annotations

from tlde.ingest.cache import combined_key
from tlde.ingest.datasheet_model import (
    DatasheetModel,
    MemoryRegion,
    Peripheral,
    PinConnection,
    Target,
)

# Map the config precedence tokens to citation/source_type values.
PRECEDENCE_TOKEN_TO_TYPE = {
    "svd": "svd",
    "dts": "dts",
    "vendor_header": "header",
    "renode_upstream": "renode",
    "reference_manual": "pdf",
    "pdf": "pdf",
    "vision": "vision",
    "web": "web",
}


def _merge_peripheral(into: Peripheral, lower: Peripheral) -> None:
    """Fold a lower-precedence peripheral into an existing higher one."""
    into.citations.extend(lower.citations)
    if not into.base_address and lower.base_address:
        into.base_address = lower.base_address
    if not into.size and lower.size:
        into.size = lower.size
    if not into.irqs and lower.irqs:
        into.irqs = lower.irqs
    if not into.group and lower.group:
        into.group = lower.group
    if not into.description and lower.description:
        into.description = lower.description
    if not into.renode_model_hint and lower.renode_model_hint:
        into.renode_model_hint = lower.renode_model_hint
    if not into.registers and lower.registers:
        into.registers = lower.registers


def merge(fragments: list[DatasheetModel]) -> DatasheetModel:
    """Merge fragments ordered HIGHEST precedence first into one model."""
    result = DatasheetModel()
    periph_by_name: dict[str, Peripheral] = {}
    mem_by_key: dict[tuple, MemoryRegion] = {}
    pins_by_net: dict[str, PinConnection] = {}
    target = Target()

    for frag in fragments:
        # target: first non-empty field wins (sources are highest-first)
        for field in ("board", "soc", "arch", "cpu_core"):
            if getattr(target, field) is None and getattr(frag.target, field) is not None:
                setattr(target, field, getattr(frag.target, field))

        for p in frag.peripherals:
            key = p.name.lower()
            if key in periph_by_name:
                _merge_peripheral(periph_by_name[key], p)
            else:
                periph_by_name[key] = p.model_copy(deep=True)

        for m in frag.memory_map:
            key = (m.kind, m.base_address)
            if key in mem_by_key:
                mem_by_key[key].citations.extend(m.citations)
            else:
                mem_by_key[key] = m.model_copy(deep=True)

        for c in frag.connectivity:
            if c.net in pins_by_net:
                pins_by_net[c.net].citations.extend(c.citations)
            else:
                pins_by_net[c.net] = c.model_copy(deep=True)

        result.provenance.extend(frag.provenance)

    result.target = target
    result.peripherals = sorted(periph_by_name.values(), key=lambda p: p.base_address)
    result.memory_map = sorted(mem_by_key.values(), key=lambda m: m.base_address)
    result.connectivity = sorted(pins_by_net.values(), key=lambda c: c.net)
    result.content_hash = combined_key([pr.content_hash for pr in result.provenance])
    result.recompute_coverage()
    return result
