"""Vendor C header loader: ``#define ..._BASE`` and IRQn enums -> fragment.

A pragmatic, target-agnostic extractor for the two highest-signal, reliably
parseable facts in CMSIS-style device headers: peripheral base addresses and
interrupt numbers. Full register-struct decoding is intentionally out of scope
(SVD covers that far more reliably).
"""

from __future__ import annotations

import re
from pathlib import Path

from tlde.ingest.cache import hash_file
from tlde.ingest.datasheet_model import (
    Citation,
    DatasheetModel,
    Peripheral,
    SourceProvenance,
)

HEADER_TRUST_TIER = 1  # vendor-authored

# #define UART0_BASE 0x40002000UL    /  #define UART0_BASE_ADDR (0x40002000U)
_BASE_RE = re.compile(
    r"#define\s+(?P<name>[A-Za-z_]\w*?)_BASE(?:_ADDR|ADDR)?\s+"
    r"\(?\s*(?P<addr>0[xX][0-9A-Fa-f]+)",
    re.MULTILINE,
)
# WIDGET0_IRQn = 5,   (inside an IRQn_Type enum)
_IRQN_RE = re.compile(
    r"(?P<name>[A-Za-z_]\w*?)_IRQn\s*=\s*(?P<num>-?\d+)",
    re.MULTILINE,
)


def load(path: str | Path, trust_tier: int = HEADER_TRUST_TIER) -> DatasheetModel:
    path = str(path)
    text = Path(path).read_text(errors="ignore")

    bases: dict[str, int] = {}
    for m in _BASE_RE.finditer(text):
        bases[m.group("name")] = int(m.group("addr"), 16)

    irqs: dict[str, int] = {}
    for m in _IRQN_RE.finditer(text):
        num = int(m.group("num"))
        if num >= 0:  # negative IRQns are Cortex-M core exceptions, not peripherals
            irqs[m.group("name")] = num

    names = set(bases) | set(irqs)
    peripherals: list[Peripheral] = []
    for name in sorted(names):
        if name not in bases:
            continue  # an IRQn with no base address isn't a placeable peripheral
        cite = Citation(
            source=path, source_type="header", trust_tier=trust_tier, section=name,
        )
        peripherals.append(Peripheral(
            name=name,
            base_address=bases[name],
            size=0,  # headers don't give the span; merge/verifier may fill from SVD
            irqs=[irqs[name]] if name in irqs else [],
            registers=[],
            citations=[cite],
        ))

    model = DatasheetModel(
        peripherals=peripherals,
        provenance=[SourceProvenance(
            source=path, source_type="header", trust_tier=trust_tier,
            content_hash=hash_file(path),
            note=f"{len(bases)} base addrs, {len(irqs)} IRQns",
        )],
    )
    model.recompute_coverage()
    return model
