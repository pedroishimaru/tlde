"""CMSIS-SVD loader: SVD XML -> DatasheetModel fragment.

SVD is the highest-signal grounding source — it is the vendor's own machine-
readable register map (addresses, offsets, reset values, bit-fields, IRQs). This
loader is fully vendor/target-agnostic: it reflects whatever the SVD declares.
"""

from __future__ import annotations

from pathlib import Path

from cmsis_svd.parser import SVDParser

from tlde.ingest.cache import hash_file
from tlde.ingest.datasheet_model import (
    BitField,
    Citation,
    DatasheetModel,
    Peripheral,
    Register,
    SourceProvenance,
    Target,
)

SVD_TRUST_TIER = 1  # vendor machine-readable

_ACCESS_MAP = {
    "READ_ONLY": "ro",
    "WRITE_ONLY": "wo",
    "READ_WRITE": "rw",
    "READ_WRITE_ONCE": "rw-once",
    "WRITE_ONCE": "w-once",
}


def _norm_access(access) -> str | None:
    """Normalise an SVD access enum to a compact token (ro/rw/wo/...)."""
    if access is None:
        return None
    name = getattr(access, "name", None)
    if name:
        return _ACCESS_MAP.get(name, name.lower())
    return str(access)


def _peripheral_size(svd_periph) -> int:
    """Address-space span: widest address block, else a conservative default."""
    blocks = getattr(svd_periph, "address_blocks", None) or []
    spans = [
        (blk.offset or 0) + (blk.size or 0)
        for blk in blocks
        if getattr(blk, "size", None)
    ]
    if spans:
        return max(spans)
    return 0x1000  # unknown block size; gate/verifier can flag


def _fields(svd_reg, cite: Citation) -> list[BitField]:
    fields: list[BitField] = []
    for f in svd_reg.get_fields() or []:
        if getattr(f, "is_reserved", False):
            continue
        enum = {}
        if getattr(f, "is_enumerated_type", False):
            for ev in (f.enumerated_values or []):
                for entry in (getattr(ev, "enumerated_values", None) or []):
                    if entry.name is not None and entry.value is not None:
                        enum[entry.name] = entry.value
        fields.append(
            BitField(
                name=f.name,
                bit_offset=f.bit_offset if f.bit_offset is not None else 0,
                bit_width=f.bit_width if f.bit_width is not None else 1,
                access=_norm_access(f.access),
                description=(f.description or None),
                enum_values=enum,
                citations=[cite.model_copy(update={"note": f"field {f.name}"})],
            )
        )
    return fields


def _registers(svd_periph, source: str) -> list[Register]:
    regs: list[Register] = []
    for r in svd_periph.get_registers() or []:
        cite = Citation(
            source=source,
            source_type="svd",
            trust_tier=SVD_TRUST_TIER,
            section=f"{svd_periph.name}.{r.name}",
        )
        regs.append(
            Register(
                name=r.name,
                offset=r.address_offset or 0,
                size_bits=r.size or svd_periph.size or 32,
                access=_norm_access(r.access) or "rw",
                reset_value=r.reset_value,
                description=(r.description or None),
                bit_fields=_fields(r, cite),
                citations=[cite],
            )
        )
    return regs


def load(path: str | Path, trust_tier: int = SVD_TRUST_TIER) -> DatasheetModel:
    """Parse an SVD file into a DatasheetModel fragment."""
    path = str(path)
    device = SVDParser.for_xml_file(path).get_device()

    peripherals: list[Peripheral] = []
    for p in device.peripherals:
        cite = Citation(
            source=path,
            source_type="svd",
            trust_tier=trust_tier,
            section=p.name,
        )
        irqs = sorted({i.value for i in (p.interrupts or []) if i.value is not None})
        peripherals.append(
            Peripheral(
                name=p.name,
                group=(p.group_name or None),
                base_address=p.base_address,
                size=_peripheral_size(p),
                irqs=irqs,
                registers=_registers(p, path),
                description=(p.description or None),
                citations=[cite],
            )
        )

    model = DatasheetModel(
        target=Target(
            soc=(getattr(device, "name", None) or None),
            cpu_core=(getattr(getattr(device, "cpu", None), "name", None) or None),
        ),
        peripherals=peripherals,
        provenance=[
            SourceProvenance(
                source=path,
                source_type="svd",
                trust_tier=trust_tier,
                content_hash=hash_file(path),
                note=f"{len(peripherals)} peripherals",
            )
        ],
    )
    model.recompute_coverage()
    return model
