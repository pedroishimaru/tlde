"""Classify a headless Renode run into pass / model-defect / other.

Heuristics over the Renode log + UART distinguish *model defects* (route back to
the engineer to re-ground a fix) from firmware/harness issues (do not route). An
optional LLM classifier (the fw_failure_classifier role) adjudicates only when
the heuristics are inconclusive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tlde.binloop.metadata import BinaryMeta
from tlde.binloop.runner import RunResult

# Ordered by severity; first match wins. All of these are MODEL defects.
_MODEL_DEFECT_PATTERNS: list[tuple[str, str]] = [
    ("unmapped_sysbus_access",
     r"(WriteToUnmapped|ReadFromUnmapped|unmapped|non-existing peripheral|"
     r"no peripheral (found )?at)"),
    ("unimplemented_register",
     r"(Tag\b|tagged|unhandled (read|write)|unimplemented register)"),
    ("peripheral_not_registered",
     r"(could not find peripheral|peripheral .* not registered|no peripheral registered)"),
    ("wrong_size_or_range",
     r"(out of range|outside (of )?the bounds|exceeds the size|access .* out of)"),
    ("missing_or_miswired_irq",
     r"(spurious interrupt|unhandled interrupt|no handler for|interrupt .* not connected)"),
    ("cpu_fault_or_boot",
     r"(HardFault|hard fault|lockup|PC does not lay in memory|CPU was halted|"
     r"escalated to hardfault)"),
]

_ADDR_RE = re.compile(r"0x[0-9A-Fa-f]+")


@dataclass
class Classification:
    passed: bool
    tier_reached: int                 # 0 fail / 1 boot-ok / 2 markers-matched
    model_defect: bool
    category: str | None = None
    evidence: list[str] = field(default_factory=list)
    address: int | None = None
    peripheral_guess: str | None = None
    summary: str = ""
    log: str = ""                     # captured run log tail (for LLM refinement)
    uart: str = ""                    # captured console tail

    @property
    def ambiguous(self) -> bool:
        """True when heuristics were inconclusive (worth an LLM second opinion)."""
        return not self.passed and (
            self.category is None or self.category == "wrong_reset_value_spin"
        )


def _evidence_lines(log: str, pattern: str, limit: int = 4) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [ln.strip() for ln in log.splitlines() if rx.search(ln)][:limit]


def _first_defect(log: str) -> tuple[str, list[str]] | None:
    for category, pattern in _MODEL_DEFECT_PATTERNS:
        lines = _evidence_lines(log, pattern)
        if lines:
            return category, lines
    return None


def _markers_present(uart: str, markers: list[str]) -> bool:
    return all(m in uart for m in markers) if markers else False


def classify(
    result: RunResult,
    meta: BinaryMeta,
    address_to_peripheral=None,    # optional callable(int) -> name|None
    llm_classify=None,             # optional sync callable(log, uart, meta) -> dict
) -> Classification:
    """Classify one run, attaching log/UART tails for any later LLM refinement."""
    cls = _classify_core(result, meta, address_to_peripheral, llm_classify)
    cls.log = (result.log or "")[-8000:]
    cls.uart = (result.uart or "")[-2000:]
    return cls


def _classify_core(
    result: RunResult,
    meta: BinaryMeta,
    address_to_peripheral=None,
    llm_classify=None,
) -> Classification:
    """``address_to_peripheral`` maps a faulting address to a peripheral name
    (e.g. via the DatasheetModel) to focus the fix."""
    if not result.ran:
        return Classification(passed=False, tier_reached=0, model_defect=False,
                              category=None, evidence=[result.error],
                              summary=f"run did not execute: {result.error}")

    log, uart = result.log, result.uart
    defect = _first_defect(log)

    if defect is not None:
        category, evidence = defect
        addr = None
        for ln in evidence:
            m = _ADDR_RE.search(ln)
            if m:
                addr = int(m.group(0), 16)
                break
        periph = address_to_peripheral(addr) if (address_to_peripheral and addr) else None
        return Classification(
            passed=False, tier_reached=0, model_defect=True, category=category,
            evidence=evidence, address=addr, peripheral_guess=periph,
            summary=f"model defect: {category}"
            + (f" near {hex(addr)}" if addr else "")
            + (f" (peripheral {periph})" if periph else ""),
        )

    # Any emulator-level error that isn't a known defect ⇒ a run error, NOT a
    # pass (e.g. a failed ELF/platform load). Renode prints monitor command
    # errors without a [LEVEL] prefix, so match those too. Conservative: never
    # pass on errors.
    _ERR_MARKERS = (
        "[ERROR]", "There was an error executing command", "Error while loading",
        "Could not load", "Unhandled exception", "Exception was thrown",
        "could not be loaded",
    )
    error_lines = [ln.strip() for ln in log.splitlines()
                   if any(mk in ln for mk in _ERR_MARKERS)]
    if error_lines:
        return Classification(
            passed=False, tier_reached=0, model_defect=False, category=None,
            evidence=error_lines[:4],
            summary="run error (see log): " + error_lines[0].strip()[:120],
        )

    # No model-defect signal in the log.
    tier2_required = meta.success.tier >= 2 and bool(meta.success.expect_console)
    markers_ok = _markers_present(uart, meta.success.expect_console)

    if result.timed_out:
        # Ran to the wall-clock guard without a clean quit and without markers:
        # likely a polling spin (e.g. wrong reset value). Low confidence → LLM.
        cls = Classification(
            passed=False, tier_reached=0, model_defect=True,
            category="wrong_reset_value_spin",
            evidence=["wall-clock timeout with no console markers / clean exit"],
            summary="suspected spin (timed out, no progress)",
        )
        if llm_classify is not None:
            cls = _apply_llm(cls, llm_classify, log, uart, meta)
        return cls

    if tier2_required and not markers_ok:
        # Booted cleanly but the golden markers never appeared — could be a subtle
        # model gap or a firmware/harness issue. Defer to the LLM if available.
        cls = Classification(
            passed=False, tier_reached=1, model_defect=False,
            category=None,
            evidence=[f"expected console markers not found: {meta.success.expect_console}"],
            summary="booted, tier-2 markers not matched",
        )
        if llm_classify is not None:
            cls = _apply_llm(cls, llm_classify, log, uart, meta)
        return cls

    # Passed: clean boot to the virtual-time budget, no defects; markers matched
    # when required.
    tier = 2 if (tier2_required and markers_ok) else 1
    return Classification(passed=True, tier_reached=tier, model_defect=False,
                          summary=f"passed (tier {tier})")


def _apply_llm(base: Classification, llm_classify, log, uart, meta) -> Classification:
    """Let the LLM classifier refine an inconclusive verdict."""
    try:
        out = llm_classify(log, uart, meta) or {}
    except Exception:
        return base
    cat = out.get("category")
    if cat:
        base.category = cat
        base.model_defect = bool(out.get("model_defect", base.model_defect))
        base.peripheral_guess = out.get("peripheral_guess", base.peripheral_guess)
        if out.get("evidence"):
            base.evidence = list(out["evidence"])
        base.summary = f"LLM classifier: {cat}"
    return base
