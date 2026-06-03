"""Zephyr/Devicetree loader: a resolved .dts -> DatasheetModel fragment.

DTS is a strong, machine-readable confirmation source for base addresses, IRQs,
memory regions, and board-level wiring (LEDs/buttons). It carries no register
maps, so it complements SVD rather than replacing it. Fully target-agnostic:
everything is read from the tree, nothing is assumed about a vendor.

Pass a *resolved* devicetree (e.g. Zephyr's build-output ``zephyr.dts``) so all
phandles and includes are already expanded.
"""

from __future__ import annotations

from pathlib import Path

from devicetree import dtlib

from tlde.ingest.cache import hash_file
from tlde.ingest.datasheet_model import (
    Citation,
    DatasheetModel,
    MemoryRegion,
    Peripheral,
    PinConnection,
    SourceProvenance,
)

DTS_TRUST_TIER = 2


def _cells(node, prop_name: str, default: int) -> int:
    """Read an #*-cells count from the node's parent chain, else a default."""
    n = node.parent
    while n is not None:
        if prop_name in n.props:
            try:
                return n.props[prop_name].to_nums()[0]
            except Exception:
                break
        n = n.parent
    return default


def _combine(cells: list[int]) -> int:
    """Combine 32-bit DT cells (big-endian order) into one integer."""
    value = 0
    for c in cells:
        value = (value << 32) | (c & 0xFFFFFFFF)
    return value


def _prop_u32s(prop) -> list[int]:
    """Decode any property's raw bytes into big-endian u32 cells.

    Works for phandle-bearing props (e.g. ``gpios = <&ctrl pin flags>``) where
    ``to_nums()`` refuses to parse — in a resolved DTS the phandle is the
    referenced node's numeric ``phandle`` value.
    """
    b = prop.value
    return [int.from_bytes(b[i : i + 4], "big") for i in range(0, len(b) - 3, 4)]


def _reg_pairs(node) -> list[tuple[int, int]]:
    """Decode a node's ``reg`` into (address, size) pairs honouring cell sizes."""
    if "reg" not in node.props:
        return []
    a = _cells(node, "#address-cells", 1)
    s = _cells(node, "#size-cells", 1)
    try:
        nums = node.props["reg"].to_nums()
    except Exception:
        return []
    stride = a + s
    pairs: list[tuple[int, int]] = []
    for i in range(0, len(nums) - stride + 1, stride):
        addr = _combine(nums[i : i + a])
        size = _combine(nums[i + a : i + a + s]) if s else 0
        pairs.append((addr, size))
    return pairs


def _status_ok(node) -> bool:
    if "status" not in node.props:
        return True
    try:
        return node.props["status"].to_string() not in ("disabled", "reserved")
    except Exception:
        return True


def _node_name(node) -> str:
    if node.labels:
        return node.labels[0]
    return node.name.split("@", 1)[0]


def _compatibles(node) -> list[str]:
    if "compatible" not in node.props:
        return []
    try:
        return list(node.props["compatible"].to_strings())
    except Exception:
        return []


def _memory_kind(node, compats: list[str]) -> str | None:
    """Classify a reg-bearing node as flash/ram memory, or None (a peripheral).

    Deliberately conservative so device *controllers* (e.g. a flash controller)
    are not mistaken for memory regions.
    """
    joined = " ".join(compats).lower()
    name = node.name.lower()
    dt = node.props.get("device_type")
    is_mem = False
    if dt is not None:
        try:
            is_mem = dt.to_string() == "memory"
        except Exception:
            pass
    if any(k in joined for k in ("soc-nv-flash", "nv-flash", "jedec,spi-nor", "mtd")):
        return "flash"
    if is_mem:
        return "ram"
    # Bare memory nodes typically carry no compatible.
    if not compats and (name.startswith(("memory", "sram", "ram", "dram", "ddr"))):
        return "ram"
    return None


def _phandle_map(dt: dtlib.DT) -> dict[int, "dtlib.Node"]:
    mapping: dict[int, dtlib.Node] = {}
    for node in dt.node_iter():
        if "phandle" in node.props:
            try:
                mapping[node.props["phandle"].to_nums()[0]] = node
            except Exception:
                pass
    return mapping


def _gpio_connections(dt: dtlib.DT, source: str, phandles: dict) -> list[PinConnection]:
    """Best-effort LED/button wiring from gpio-leds / gpio-keys nodes."""
    conns: list[PinConnection] = []
    for node in dt.node_iter():
        compats = _compatibles(node)
        kind = None
        if any("gpio-leds" in c for c in compats):
            kind = "led"
        elif any("gpio-keys" in c for c in compats):
            kind = "button"
        if kind is None:
            continue
        for child in node.nodes.values():
            gp = child.props.get("gpios")
            if gp is None:
                continue
            raw = _prop_u32s(gp)  # [phandle, pin, flags, ...]
            if len(raw) < 2:
                continue
            ctrl = phandles.get(raw[0])
            ctrl_name = _node_name(ctrl) if ctrl is not None else f"phandle:{raw[0]}"
            pin = raw[1]
            flags = raw[2] if len(raw) > 2 else 0
            label = child.props["label"].to_string() if "label" in child.props else _node_name(child)
            conns.append(
                PinConnection(
                    net=label,
                    soc_pin=f"{ctrl_name}.{pin}",
                    peripheral=ctrl_name,
                    function=f"gpio-{kind}",
                    active_low=bool(flags & 0x1),
                    citations=[Citation(
                        source=source, source_type="dts",
                        trust_tier=DTS_TRUST_TIER, section=child.path,
                    )],
                )
            )
    return conns


def load(path: str | Path, trust_tier: int = DTS_TRUST_TIER) -> DatasheetModel:
    path = str(path)
    dt = dtlib.DT(path)
    phandles = _phandle_map(dt)

    peripherals: list[Peripheral] = []
    memory_map: list[MemoryRegion] = []

    for node in dt.node_iter():
        if node.parent is None or not _status_ok(node):
            continue
        pairs = _reg_pairs(node)
        if not pairs:
            continue
        compats = _compatibles(node)
        cite = Citation(
            source=path, source_type="dts", trust_tier=trust_tier, section=node.path,
        )
        mem_kind = _memory_kind(node, compats)
        if mem_kind is not None:
            base, size = pairs[0]
            memory_map.append(MemoryRegion(
                name=_node_name(node), base_address=base, size=size,
                kind=mem_kind, citations=[cite],
            ))
            continue
        if not compats:
            continue  # plain addressed node w/o a device class — skip
        base, size = pairs[0]
        irqs: list[int] = []
        if "interrupts" in node.props:
            try:
                icells = _cells(node, "#interrupt-cells", 2)
                nums = node.props["interrupts"].to_nums()
                # 3-cell GIC = (type, num, flags); else first cell is the IRQ number.
                idx = 1 if icells >= 3 else 0
                irqs = [nums[i] for i in range(idx, len(nums), max(icells, 1))]
            except Exception:
                irqs = []
        peripherals.append(Peripheral(
            name=_node_name(node), base_address=base, size=size,
            irqs=sorted(set(irqs)), registers=[],
            description=(compats[0] if compats else None),
            citations=[cite],
        ))

    connectivity = _gpio_connections(dt, path, phandles)

    model = DatasheetModel(
        peripherals=peripherals,
        memory_map=memory_map,
        connectivity=connectivity,
        provenance=[SourceProvenance(
            source=path, source_type="dts", trust_tier=trust_tier,
            content_hash=hash_file(path),
            note=f"{len(peripherals)} nodes, {len(memory_map)} memory, {len(connectivity)} pins",
        )],
    )
    model.recompute_coverage()
    return model
