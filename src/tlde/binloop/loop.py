"""Binary-in-the-loop self-correction.

For each prebuilt binary: generate a .resc, run Renode headless, classify the
outcome. When the failure is a *model defect*, route a structured report to the
engineer, who must re-ground the fix in tlde-kb (the binary says *what* is wrong;
the docs say the correct *value*). Guards:

  * overfitting — enforced by the report instruction + the verifier's citation
    requirement (no uncited "magic" values).
  * regression — after a fix, re-run every previously-passing binary whose
    peripherals were touched; a regression rolls the fix back.
  * termination — stop at max_attempts, on no-progress (same defect twice), or
    when all binaries pass.

This stage *complements* the doc-grounded Verifier and the samples-build Tester:
the Verifier remains authoritative for value correctness, the samples Robot
tests remain the primary functional gate, and this loop adds an empirical signal.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tlde.binloop.classify import Classification, classify
from tlde.binloop.metadata import BinaryMeta, discover
from tlde.binloop.report import BinaryReport, failure_report
from tlde.binloop.resc import generate_resc, write_resc
from tlde.binloop.runner import RunResult, run


def address_to_peripheral_fn(model):
    """Return a callable mapping a faulting address to a peripheral name."""
    def lookup(addr: int | None) -> str | None:
        if addr is None or model is None:
            return None
        for p in model.peripherals:
            if p.base_address <= addr < p.base_address + max(p.size, 1):
                return p.name
        return None
    return lookup


@dataclass
class EvalContext:
    cfg: object
    output_dir: str
    repl_files: list[str]
    cs_files: list[str]
    addr2p: object
    classifier=None             # optional LLM classifier callable


def evaluate(meta: BinaryMeta, ctx: EvalContext) -> Classification:
    """Generate the .resc, run headless, and classify (one attempt)."""
    work = Path(ctx.output_dir) / "binloop" / meta.name
    work.mkdir(parents=True, exist_ok=True)
    uart_log = str(work / "uart.log")
    if Path(uart_log).exists():
        Path(uart_log).unlink()
    resc_text = generate_resc(meta, ctx.repl_files, ctx.cs_files, uart_log_path=uart_log)
    resc_path = write_resc(resc_text, work / "run.resc")
    result: RunResult = run(
        resc_path,
        renode_bin=ctx.cfg.binaries.renode_bin,
        wall_timeout_s=ctx.cfg.binaries.wall_timeout_s,
        uart_log_path=uart_log,
    )
    return classify(result, meta, address_to_peripheral=ctx.addr2p,
                    llm_classify=ctx.classifier)


def _snapshot(output_dir: str) -> str:
    tmp = tempfile.mkdtemp(prefix="tlde-binloop-snap-")
    shutil.copytree(output_dir, Path(tmp) / "out", dirs_exist_ok=True)
    return tmp


def _restore(output_dir: str, snap: str) -> None:
    src = Path(snap) / "out"
    if src.is_dir():
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.copytree(src, output_dir)


async def _maybe_refine(c: Classification, refine) -> Classification:
    """Ask the async LLM classifier to refine an ambiguous verdict, if provided."""
    if refine is not None and c.ambiguous:
        try:
            return await refine(c)
        except Exception:
            return c
    return c


async def run_binary_loop(
    cfg, board: str, output_dir: str, model=None,
    engineer_revise=None,          # async callable(failure_report: dict) -> set[str]
    refine=None,                   # async callable(Classification) -> Classification
    evaluate_fn=evaluate,          # injectable for tests
) -> list[BinaryReport]:
    """Run the loop over all binaries for ``board``. Returns per-binary reports."""
    metas = discover(cfg.binaries.dir, board)
    if not metas:
        return []

    out = Path(output_dir)
    repl_files = sorted(str(p) for p in out.glob("*.repl"))
    cs_files = sorted(str(p) for p in out.glob("*.cs"))
    # LLM refinement happens via the async `refine` hook; ctx.classifier stays None.
    ctx = EvalContext(cfg=cfg, output_dir=output_dir, repl_files=repl_files,
                      cs_files=cs_files, addr2p=address_to_peripheral_fn(model))

    reports: dict[str, BinaryReport] = {m.name: BinaryReport(name=m.name) for m in metas}
    meta_by_name = {m.name: m for m in metas}

    # Initial evaluation pass.
    from tlde.progress import track
    for meta in track(metas, total=len(metas), desc="[Phase 3] binaries", unit="bin"):
        c = await _maybe_refine(evaluate_fn(meta, ctx), refine)
        rep = reports[meta.name]
        rep.attempts = 1
        rep.update_from(c)

    passing = {n for n, r in reports.items() if r.passed}
    max_attempts = cfg.binaries.max_attempts

    if engineer_revise is None:
        return list(reports.values())  # detection-only (no LLM to revise)

    # Self-correction with regression + no-progress guards.
    for meta in metas:
        rep = reports[meta.name]
        last_category = rep.residual_category
        while (not rep.passed and rep.attempts < max_attempts
               and rep.residual_category is not None):
            c = evaluate_fn(meta, ctx)  # refresh current state
            if c.passed:
                rep.update_from(c)
                passing.add(meta.name)
                break
            c = await _maybe_refine(c, refine)
            snap = _snapshot(output_dir)
            changed = await engineer_revise(failure_report(meta, c)) or set()
            rep.changed_peripherals = sorted(set(rep.changed_peripherals) | set(changed))
            # refresh artifact lists (engineer may add files)
            ctx.repl_files = sorted(str(p) for p in out.glob("*.repl"))
            ctx.cs_files = sorted(str(p) for p in out.glob("*.cs"))

            c2 = evaluate_fn(meta, ctx)
            rep.attempts += 1
            rep.update_from(c2)

            # Regression guard: re-run previously-passing binaries touched by the fix.
            regressed = _regressions(passing, meta.name, changed, meta_by_name,
                                     reports, ctx, evaluate_fn)
            if regressed:
                _restore(output_dir, snap)
                rep.regressed = True
                rep.history.append(f"fix rolled back (regressed: {', '.join(regressed)})")
                break

            if c2.passed:
                passing.add(meta.name)
                break
            if c2.category == last_category:  # no progress
                rep.history.append("no progress (same defect) — stopping")
                break
            last_category = c2.category

    return list(reports.values())


def _regressions(passing, current, changed, meta_by_name, reports, ctx, evaluate_fn) -> list[str]:
    """Re-run previously-passing binaries; return names that now fail."""
    regressed: list[str] = []
    for name in list(passing):
        if name == current:
            continue
        c = evaluate_fn(meta_by_name[name], ctx)
        if not c.passed:
            regressed.append(name)
    return regressed
