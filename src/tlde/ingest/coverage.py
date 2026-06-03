"""Coverage gates — the fail-loud replacement for the old fails-open behaviour.

Decides whether a built DatasheetModel + retrieval corpus is trustworthy enough
to proceed. Honours ``ingest.strictness``:
    fail_closed  — abort on corrupt/zero-yield/empty/low structured coverage
    quarantine   — proceed, but ungrounded peripherals are flagged (block pass)
    warn         — proceed with warnings only
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tlde.ingest.datasheet_model import DatasheetModel

STRUCTURED_TYPES = {"svd", "dts", "header", "renode", "vision"}


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)  # ungrounded peripheral names


def _has_structured_source(model: DatasheetModel) -> bool:
    return any(p.source_type in STRUCTURED_TYPES for p in model.provenance)


def decide(
    model: DatasheetModel,
    retrieval_chunks: int,
    strictness: str = "fail_closed",
    min_coverage: float = 0.7,
) -> GateResult:
    """Evaluate the gate. ``retrieval_chunks`` is the indexed page-anchored count."""
    cov = model.recompute_coverage()
    warnings: list[str] = list(cov.warnings)
    ungrounded = [p.name for p in model.peripherals if not p.grounded]

    has_structured = _has_structured_source(model)
    has_any_material = (not model.is_empty()) or retrieval_chunks > 0

    # Hard failures (apply under fail_closed and quarantine; warn just records them).
    hard_reason = ""
    if not has_any_material:
        hard_reason = (
            "no grounded material: structured model is empty and no retrievable "
            "text/tables were indexed. Provide an SVD/DTS/header or a text PDF, "
            "enable vision/web-research, or lower ingest.strictness."
        )
    elif has_structured and cov.overall_coverage < min_coverage:
        hard_reason = (
            f"structured coverage {cov.overall_coverage:.0%} is below the "
            f"min_coverage gate ({min_coverage:.0%}): "
            f"{cov.peripherals_grounded}/{cov.peripherals_total} peripherals and "
            f"{cov.registers_grounded}/{cov.registers_total} registers are cited."
        )

    if not has_structured and retrieval_chunks > 0:
        warnings.append(
            "no machine-readable structured source (SVD/DTS/header): grounding is "
            "text-only via retrieval — lower precision, citations are page-anchored."
        )
    if ungrounded:
        warnings.append(
            f"{len(ungrounded)} peripheral(s) lack citations (ungrounded): "
            + ", ".join(ungrounded[:8]) + ("…" if len(ungrounded) > 8 else "")
        )

    if hard_reason:
        if strictness == "fail_closed":
            return GateResult(ok=False, reason=hard_reason, warnings=warnings)
        if strictness == "quarantine":
            return GateResult(ok=True, reason=hard_reason, warnings=warnings,
                              quarantined=ungrounded)
        # warn
        warnings.insert(0, f"GATE (warn-only): {hard_reason}")
        return GateResult(ok=True, warnings=warnings, quarantined=ungrounded)

    if strictness == "quarantine":
        return GateResult(ok=True, warnings=warnings, quarantined=ungrounded)
    return GateResult(ok=True, warnings=warnings)
