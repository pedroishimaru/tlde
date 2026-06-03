"""Per-binary reports + the structured failure report routed to the engineer."""

from __future__ import annotations

from dataclasses import dataclass, field

from tlde.binloop.classify import Classification
from tlde.binloop.metadata import BinaryMeta

OVERFIT_GUARD = (
    "Look up the correct value in tlde-kb (get_register/get_peripheral) and "
    "re-ground the fix in a citation. Do NOT hack a value just to make this "
    "binary pass."
)


def failure_report(meta: BinaryMeta, c: Classification) -> dict:
    """Structured report handed to the engineer: the binary says *what* is wrong."""
    return {
        "binary": meta.name,
        "symptom": c.category,
        "evidence": c.evidence,
        "address": (hex(c.address) if c.address is not None else None),
        "peripheral_guess": c.peripheral_guess,
        "instruction": OVERFIT_GUARD,
    }


@dataclass
class BinaryReport:
    name: str
    passed: bool = False
    tier_reached: int = 0
    attempts: int = 0
    boot_reached: bool = False
    markers_hit: bool = False
    residual_category: str | None = None
    residual_summary: str = ""
    changed_peripherals: list[str] = field(default_factory=list)
    regressed: bool = False
    history: list[str] = field(default_factory=list)

    def update_from(self, c: Classification) -> None:
        self.passed = c.passed
        self.tier_reached = c.tier_reached
        self.boot_reached = c.tier_reached >= 1 or c.passed
        self.markers_hit = c.tier_reached >= 2
        self.residual_category = None if c.passed else c.category
        self.residual_summary = c.summary
        self.history.append(c.summary)


def summarize(reports: list[BinaryReport]) -> str:
    passed = sum(1 for r in reports if r.passed)
    lines = [f"Binary-in-the-loop: {passed}/{len(reports)} binaries passing"]
    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        extra = "" if r.passed else f" — residual: {r.residual_summary}"
        reg = " [REGRESSION]" if r.regressed else ""
        lines.append(f"  [{status}] {r.name} (tier {r.tier_reached}, "
                     f"{r.attempts} attempt(s)){extra}{reg}")
    return "\n".join(lines)
