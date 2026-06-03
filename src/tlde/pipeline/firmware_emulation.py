"""Firmware emulation pipeline.

Runs the full emulation workflow:
  Phase 1: Manager decomposes MCU specs into work units.
  Phase 2: Engineer–Verifier loops per work unit (parallel where deps allow).
           Each unit gets an engineer that builds artifacts, then a verifier
           that cross-checks against vendor docs. Mismatches loop back to the
           engineer for revision, up to concurrency.verify_retries times.
  Phase 3: Test aggregator builds firmware samples, runs Robot Framework tests.
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tlde import progress
from tlde import settings
from tlde.agent import (
    run_agent,
    run_agent_interactive,
    ModelUnavailableError,
    _approve_all_handler,
    _test_permission_handler,
)
from tlde.agents import AGENTS
from tlde.ingest import build_datasheet_model, IngestError
from tlde.observability import PipelineTrace
from tlde.rag import KnowledgeBase


@dataclass
class WorkUnitResult:
    """Outcome of the engineer–verifier loop for one work unit."""

    name: str
    engineer_response: str
    verifier_response: str
    verified: bool
    attempts: int
    skipped: bool = False
    skip_reason: str = ""


async def main():
    if len(sys.argv) < 2:
        print("Usage: tlde <prompt> [--source URL_OR_PATH ...] [--plan work_plan.json]")
        print("            [--config tlde.toml] [--provider NAME] [--model NAME]")
        print('\nExample: tlde "Emulate the nRF52833" --source https://example.com/spec.pdf')
        print('Resume:  tlde "Emulate the nRF52833" --plan output/work_plan.json')
        print('Config:  tlde "Emulate the nRF52833" --provider openrouter --model z-ai/glm-5')
        sys.exit(1)

    # Parse args: bare words form the prompt; value-taking flags consume the next arg.
    args = sys.argv[1:]
    prompt_parts: list[str] = []
    sources: list[str] = []
    plan_file = None
    config_path = "tlde.toml"
    cli_overrides: dict[str, str] = {}

    expect = None  # flag currently awaiting its value
    parsing_sources = False
    for arg in args:
        if expect is not None:
            if expect == "plan":
                plan_file = arg
            elif expect == "config":
                config_path = arg
            else:  # provider | model
                cli_overrides[expect] = arg
            expect = None
            continue
        if arg == "--source":
            parsing_sources = True
        elif arg in ("--plan", "--config", "--provider", "--model"):
            parsing_sources = False
            expect = arg[2:]
        elif parsing_sources:
            sources.append(arg)
        else:
            prompt_parts.append(arg)

    user_prompt = " ".join(prompt_parts)
    if not user_prompt and not plan_file:
        print("Error: no prompt provided.")
        sys.exit(1)

    # Load configuration: CLI flags > env/.env > tlde.toml > built-in defaults.
    settings.init_settings(config_path=config_path, cli_overrides=cli_overrides)

    trace = PipelineTrace()

    # --- Phase 0: Build the grounded datasheet model (fail-closed) ---
    cfg = settings.get_settings()
    kb = KnowledgeBase(
        embedding_model=cfg.models.embedder,
        reranker_model=cfg.models.reranker if cfg.ingest.rerank else None,
        use_rerank=cfg.ingest.rerank,
    )

    # Sources come from tlde.toml [sources]; CLI --source and any URLs in the
    # prompt are merged in as extras.
    urls_in_prompt = re.findall(r'https?://[^\s"\'<>]+', user_prompt)
    extra_sources = list(dict.fromkeys(sources + urls_in_prompt))

    # Optional autonomous web research: discover + fetch missing machine-readable
    # inputs from the trust allowlist. Offline-safe (no-ops without a backend).
    if cfg.research.mode != "off":
        from tlde import research as _research
        from tlde.research.types import source_type_for
        configured = {source_type_for(s) for s in extra_sources}
        for t in ("svd", "dts", "header"):
            if getattr(cfg.sources, t, None):
                configured.add(t)
        missing = [t for t in ("svd", "dts", "header") if t not in configured]
        if missing:
            rres = await _research.gather_inputs(cfg, missing)
            for note in rres.notes:
                print(f"  [research] {note}")
            extra_sources.extend(rres.paths)

    print("[Phase 0: Ingest] Building grounded datasheet model")
    print("-" * 60)
    try:
        build = build_datasheet_model(cfg, extra_sources=extra_sources, kb=kb)
    except IngestError as e:
        print(f"[Phase 0] ABORT (fail-closed): {e}")
        print("Fix the source(s), provide an SVD/DTS, or set ingest.strictness "
              "to 'quarantine'/'warn' in tlde.toml.")
        sys.exit(1)

    # URL sources feed retrieval (structured loaders handle only local files).
    for src in extra_sources:
        if src.startswith(("http://", "https://")):
            try:
                n = await kb.ingest_source(src)
                print(f"  + {src}: {n} chunks")
            except Exception as e:
                if cfg.ingest.strictness == "fail_closed":
                    print(f"[Phase 0] ABORT (fail-closed): failed to fetch {src}: {e}")
                    sys.exit(1)
                print(f"  ! {src}: {e}")

    for w in build.gate.warnings:
        print(f"  [warn] {w}")
    if not build.gate.ok:
        print(f"[Phase 0] ABORT (fail-closed): {build.gate.reason}")
        print("Provide an SVD/DTS/header or a text PDF, enable vision/web-research, "
              "or lower ingest.strictness in tlde.toml.")
        sys.exit(1)

    m = build.model
    n_regs = sum(len(p.registers) for p in m.peripherals)
    print(f"[Phase 0: Ingest] DatasheetModel ready: {len(m.peripherals)} peripherals, "
          f"{n_regs} registers, {len(m.memory_map)} memory regions, "
          f"{len(m.connectivity)} pins")
    print(f"[Phase 0: Ingest] Grounding coverage: {m.coverage.overall_coverage:.0%} · "
          f"retrieval KB: {kb.chunk_count} chunks")

    # Phase 0b: vision augmentation — figures (pinouts/schematics/bit-fields) ->
    # structured connectivity/bit-fields, validated against DTS/SVD. Runs through
    # the configured vision-capable model; with vision="auto" it degrades quietly
    # when no vision model is available.
    if cfg.ingest.vision != "off":
        pdf_srcs = list(build.pages_by_source.keys())
        if pdf_srcs:
            from tlde.ingest import vision as _vision
            try:
                vfrag, vwarn = await _vision.augment(cfg, m, pdf_srcs)
                for w in vwarn:
                    print(f"  [vision] {w}")
                added = _vision.merge_vision(m, vfrag)
                if added:
                    print(f"[Phase 0: Vision] merged {added} vision facts "
                          f"({len(m.connectivity)} pins total)")
                    kb.ingest_structured(m)
            except Exception as e:
                print(f"  [vision] skipped: {e}")

    # Persist the grounded model + corpus and expose them to the tlde-kb MCP
    # server subprocesses (they inherit this env), so agents query structured
    # slices with citations instead of re-reading PDFs.
    from tlde.ingest.cache import write_current
    write_current(cfg.ingest.cache_dir, m, kb.export_corpus())
    os.environ["TLDE_KB_DIR"] = str(Path(cfg.ingest.cache_dir).resolve())
    print("-" * 60)

    # --- Phase 1: Manager decomposes the work (or load from cache) ---
    if plan_file:
        plan_path = Path(plan_file)
        if not plan_path.exists():
            print(f"[ERROR] Plan file not found: {plan_file}")
            sys.exit(1)
        work_plan = json.loads(plan_path.read_text())
        print(f"[Phase 1: Manager] Loaded cached work plan from {plan_file}")
    else:
        work_plan = await phase_manager(user_prompt, trace, kb)
        # Save for reuse
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        plan_path = out_dir / "work_plan.json"
        plan_path.write_text(json.dumps(work_plan, indent=2))
        print(f"[Phase 1: Manager] Work plan saved to {plan_path}")
    target = work_plan["target"]
    work_units = work_plan["work_units"]
    board = target["board"]

    # --- Phase 2: Engineer–Verifier loops (parallel where deps allow) ---
    results = await phase_engineer_verifier(target, work_units, user_prompt, trace, kb)

    # --- Phase 3: Binary-in-the-loop self-correction (complements samples) ---
    bin_reports = await phase_binary_loop(board, m, trace)

    # --- Phase 4: Test aggregator builds + runs Robot Framework tests (primary) ---
    test_report = await phase_testing(board, trace)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"Target: {board} ({target['soc']})")
    verified = sum(1 for r in results.values() if r.verified)
    failed = sum(1 for r in results.values() if not r.verified and not r.skipped)
    skipped = sum(1 for r in results.values() if r.skipped)
    print(f"Work units: {verified} verified, {failed} failed, {skipped} skipped")
    print(f"Testing: done")

    trace.finish()
    print(f"\n{trace.summary()}")


# ---------------------------------------------------------------------------
# Phase 1: Manager
# ---------------------------------------------------------------------------

async def phase_manager(
    user_prompt: str, trace: PipelineTrace, kb: KnowledgeBase,
) -> dict:
    """Manager uses RAG context to produce a structured work plan."""
    manager = AGENTS["firmware_emulation_manager"](
        **settings.for_role("firmware_emulation_manager"),
    )
    print(f"[Phase 1: Manager] Running {manager.name} (model: {manager.model})")
    print(f"[Phase 1: Manager] Prompt: {user_prompt}")
    print("-" * 60)

    prompt = build_manager_prompt(user_prompt, kb)
    work_plan = None

    def _try_parse(response: str) -> str | None:
        nonlocal work_plan
        try:
            work_plan = parse_work_plan(response)
            return None  # success — stop the loop
        except ValueError:
            pass
        # Ask the agent to try again with just the JSON
        print("[Phase 1: Manager] Response was not valid JSON, requesting retry...")
        return (
            "Your previous response was not valid JSON. "
            "Please respond now with ONLY the JSON work plan object. "
            "First character must be `{`, last must be `}`. No prose."
        )

    await run_agent_interactive(
        manager, prompt, _try_parse,
        pipeline_trace=trace,
        permission_handler=_approve_all_handler,
    )

    if work_plan is None:
        print("[ERROR] Manager failed to produce a valid JSON work plan after retries.")
        sys.exit(1)
    target = work_plan["target"]
    work_units = work_plan["work_units"]

    print(f"[Phase 1: Manager] Target: {target['board']} ({target['soc']})")
    print(f"[Phase 1: Manager] Decomposed into {len(work_units)} work units:")
    for i, unit in enumerate(work_units, 1):
        deps = ", ".join(unit["dependencies"]) if unit["dependencies"] else "none"
        print(f"  {i}. {unit['name']} (deps: {deps})")
    print("-" * 60)

    return work_plan


# ---------------------------------------------------------------------------
# Phase 2: Engineer–Verifier loops (parallel with dependency awareness)
# ---------------------------------------------------------------------------

async def phase_engineer_verifier(
    target: dict,
    work_units: list[dict],
    user_prompt: str,
    trace: PipelineTrace,
    kb: KnowledgeBase | None = None,
) -> dict[str, WorkUnitResult]:
    """Run engineer–verifier pairs, parallelizing independent work units.

    Work units are dispatched as soon as all their dependencies have completed.
    Each unit runs an engineer→verifier loop: if the verifier finds mismatches,
    the feedback is sent back to the engineer for another attempt, up to
    concurrency.verify_retries times (configurable). Concurrent LLM work is
    capped by concurrency.max_parallel_units.
    """
    board = target["board"]
    print(f"\n[Phase 2: Engineer–Verifier] Processing {len(work_units)} work units")
    print("-" * 60)

    results: dict[str, WorkUnitResult] = {}
    completed_events: dict[str, asyncio.Event] = {
        unit["name"]: asyncio.Event() for unit in work_units
    }

    # Cap concurrent LLM work; the cap is acquired only after dependency waits
    # so that a unit blocked on a dependency never holds a permit (no deadlock).
    conc = settings.get_settings().concurrency
    max_retries = conc.verify_retries
    sem = asyncio.Semaphore(conc.max_parallel_units)

    async def process_unit(unit: dict) -> WorkUnitResult:
        name = unit["name"]

        # Wait for all dependencies to complete
        for dep in unit["dependencies"]:
            if dep in completed_events:
                await completed_events[dep].wait()
            if dep in results and results[dep].skipped:
                result = WorkUnitResult(
                    name=name,
                    engineer_response="",
                    verifier_response="",
                    verified=False,
                    attempts=0,
                    skipped=True,
                    skip_reason=f"dependency '{dep}' was skipped",
                )
                results[name] = result
                completed_events[name].set()
                progress.write(f"[Phase 2] SKIPPED: {name} (dependency '{dep}' was skipped)")
                return result

        # Collect dependency context
        dep_context: dict[str, str] = {}
        for dep in unit["dependencies"]:
            if dep in results:
                dep_context[dep] = results[dep].engineer_response

        # Engineer–Verifier loop (LLM work capped by the concurrency semaphore)
        engineer_response = ""
        verifier_response = ""
        verified = False

        async with sem:
            for attempt in range(1, max_retries + 1):
                # --- Engineer ---
                progress.write(f"[Phase 2: Engineer] {name} (attempt {attempt}/{max_retries})")
                if attempt == 1:
                    eng_prompt = build_engineer_prompt(unit, target, dep_context, kb)
                else:
                    eng_prompt = build_engineer_revision_prompt(
                        unit, target, engineer_response, verifier_response, kb,
                    )

                engineer = AGENTS["fw_emu_eng"](
                    name=f"engineer-{name}-attempt{attempt}",
                    **settings.for_role("fw_emu_eng"),
                )
                engineer_response = await run_agent(
                    engineer, eng_prompt, pipeline_trace=trace,
                )
                progress.write(f"[Phase 2: Engineer] {name} built artifacts (attempt {attempt})")

                # --- Verifier ---
                progress.write(f"[Phase 2: Verifier] {name} (attempt {attempt}/{max_retries})")
                verifier = AGENTS["fw_verif_eng"](
                    name=f"verifier-{name}-attempt{attempt}",
                    **settings.for_role("fw_verif_eng"),
                )
                ver_prompt = build_unit_verifier_prompt(unit, board, user_prompt, kb)
                verifier_response = await run_agent(
                    verifier, ver_prompt, pipeline_trace=trace,
                )

                verified = check_verification_passed(verifier_response)
                if verified:
                    progress.write(f"[Phase 2: Verifier] ✔ {name} verified on attempt {attempt}")
                    break
                else:
                    progress.write(f"[Phase 2: Verifier] ✗ {name} has mismatches (attempt {attempt})")

        if not verified:
            progress.write(f"[Phase 2] ✗ {name} NOT verified after {max_retries} attempts")

        result = WorkUnitResult(
            name=name,
            engineer_response=engineer_response,
            verifier_response=verifier_response,
            verified=verified,
            attempts=attempt,
        )
        results[name] = result
        completed_events[name].set()
        return result

    # Launch all units concurrently — each waits for its own deps internally.
    # A completion bar tracks finished units; per-unit logs use progress.write
    # so they don't break the bar.
    pbar = progress.bar(len(work_units), desc="[Phase 2] work units", unit="unit")

    async def _tracked(unit: dict) -> WorkUnitResult:
        try:
            return await process_unit(unit)
        finally:
            pbar.update(1)

    tasks = [asyncio.create_task(_tracked(unit)) for unit in work_units]
    await asyncio.gather(*tasks)
    pbar.close()

    verified_count = sum(1 for r in results.values() if r.verified)
    total = len(work_units)
    print(f"\n[Phase 2] {verified_count}/{total} work units verified")
    print("-" * 60)

    return results


# ---------------------------------------------------------------------------
# Phase 3: Testing
# ---------------------------------------------------------------------------

async def phase_testing(
    board: str,
    trace: PipelineTrace,
) -> str:
    """Test aggregator builds firmware, writes Robot tests, runs them."""
    print(f"\n[Phase 3: Testing] Building and testing emulation for {board}")
    print("-" * 60)

    tester = AGENTS["emu_test_agg"](
        **settings.for_role("emu_test_agg"),
    )
    prompt = build_tester_prompt(board)
    # Restricted handler: only mkdir + make/west/renode/renode-test/robot, not approve-all.
    response = await run_agent(
        tester, prompt, pipeline_trace=trace,
        permission_handler=_test_permission_handler,
    )

    print(f"[Phase 4: Testing] Complete")
    print("-" * 60)

    return response


# ---------------------------------------------------------------------------
# Phase 3: Binary-in-the-loop self-correction (complements the samples Tester)
# ---------------------------------------------------------------------------

async def phase_binary_loop(board: str, model, trace: PipelineTrace) -> list:
    """Run prebuilt binaries headless under Renode and self-correct model defects.

    Empirical complement to the doc-grounded Verifier and the samples-build
    Tester: the binary says *what* is wrong, fixes are re-grounded in tlde-kb.
    """
    cfg = settings.get_settings()
    if not cfg.binaries.run_loop:
        return []

    from tlde import binloop

    metas = binloop.discover(cfg.binaries.dir, board)
    if not metas:
        print(f"\n[Phase 3: Binary loop] no prebuilt binaries under "
              f"{cfg.binaries.dir}/{board}/ — skipping (samples build remains primary)")
        return []

    print(f"\n[Phase 3: Binary loop] {len(metas)} binary(ies) for {board}")
    print("-" * 60)
    output_dir = f"output/{board}"
    out = Path(output_dir)

    async def engineer_revise(report: dict) -> set[str]:
        before = {p.name: p.stat().st_mtime for p in out.glob("*") if p.is_file()}
        engineer = AGENTS["fw_emu_eng"](
            name=f"binfix-{report['binary']}", **settings.for_role("fw_emu_eng"),
        )
        await run_agent(engineer, build_binloop_revision_prompt(report, board),
                        pipeline_trace=trace)
        after = {p.name: p.stat().st_mtime for p in out.glob("*") if p.is_file()}
        return {Path(n).stem for n, mt in after.items() if before.get(n) != mt}

    async def refine(c):
        agent = AGENTS["fw_failure_classifier"](
            **settings.for_role("fw_failure_classifier"),
        )
        resp = await run_agent(agent, build_classifier_prompt(c), pipeline_trace=trace)
        data = _extract_json(resp) or {}
        if data.get("category"):
            c.category = data["category"]
            c.model_defect = bool(data.get("model_defect", c.model_defect))
            c.peripheral_guess = data.get("peripheral_guess", c.peripheral_guess)
            c.summary = f"LLM classifier: {data['category']}"
        return c

    reports = await binloop.run_binary_loop(
        cfg, board, output_dir, model=model,
        engineer_revise=engineer_revise, refine=refine,
    )
    print(binloop.summarize(reports))
    out.mkdir(parents=True, exist_ok=True)
    (out / "binloop_report.json").write_text(
        json.dumps([r.__dict__ for r in reports], indent=2, default=str)
    )
    print(f"[Phase 3: Binary loop] report → {out / 'binloop_report.json'}")
    print("-" * 60)
    return reports


# ---------------------------------------------------------------------------
# Verification result parsing
# ---------------------------------------------------------------------------

def check_verification_passed(verifier_response: str) -> bool:
    """Determine if the verifier found the artifacts acceptable.

    Returns True when there are no mismatch_escalated or mismatch_fixed
    verdicts remaining (i.e. everything is verified or unverifiable).
    """
    # Try to parse the verification_report.json from the response
    report = _extract_json(verifier_response)
    if report and "summary" in report:
        summary = report["summary"]
        return (
            summary.get("mismatch_fixed", 0) == 0
            and summary.get("mismatch_escalated", 0) == 0
        )

    # Fallback: heuristic text matching
    lower = verifier_response.lower()
    if "mismatch_fixed" in lower or "mismatch_escalated" in lower:
        return False
    if "all peripherals verified" in lower or '"verified"' in lower:
        return True
    # Conservative: assume not verified if we can't tell
    return False


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text (handles code fences)."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_manager_prompt(user_prompt: str, kb: KnowledgeBase | None = None) -> str:
    """Build the user prompt for the manager agent."""
    context = ""
    if kb and kb.chunk_count > 0:
        context = kb.format_context(
            f"microcontroller peripherals memory map bus architecture "
            f"CPU core interrupt controller clock tree {user_prompt}",
            n_results=20,
        )
        context = f"\n\n{context}\n\n"

    return (
        f"{user_prompt}\n\n"
        f"Using the reference documentation below, identify the target MCU, its "
        f"peripherals, memory map, and bus architecture. Then decompose the "
        f"emulation into work units as described in your instructions."
        f"{context}"
    )


def build_engineer_prompt(
    unit: dict,
    target: dict,
    completed: dict[str, str],
    kb: KnowledgeBase | None = None,
) -> str:
    """Build a self-contained prompt for an engineer's initial attempt."""
    prompt_parts = [
        f"# Work Unit: {unit['name']}",
        f"",
        f"## Target",
        f"- Board: {target['board']}",
        f"- SoC: {target['soc']}",
        f"- Architecture: {target['architecture']}",
        f"- CPU Core: {target['cpu_core']}",
        f"",
        f"## Assignment",
        f"Renode artifact to produce: {unit['renode_artifact']}",
        f"",
        f"## Description",
        unit["description"],
        f"",
        f"## Specification References",
    ]

    for ref in unit.get("spec_references", []):
        prompt_parts.append(
            f"- {ref['document']}, {ref['section']} (pages {ref['pages']}): "
            f"{ref['content']}"
        )

    prompt_parts.append("")
    prompt_parts.append("## Key Registers")
    for reg in unit.get("key_registers", []):
        prompt_parts.append(f"- {reg['name']} (offset {reg['offset']}): {reg['purpose']}")

    if unit.get("notes"):
        prompt_parts.append("")
        prompt_parts.append(f"## Notes")
        prompt_parts.append(unit["notes"])

    if unit["dependencies"]:
        prompt_parts.append("")
        prompt_parts.append("## Context from Dependencies")
        for dep_name in unit["dependencies"]:
            dep_output = completed.get(dep_name, "")
            if dep_output and dep_output not in ("SKIPPED (tier 3)", "SKIPPED (unmet deps)"):
                prompt_parts.append(f"### {dep_name} output:")
                truncated = dep_output[:4000]
                if len(dep_output) > 4000:
                    truncated += "\n... (truncated)"
                prompt_parts.append(truncated)

    # Inject RAG context for this specific work unit
    if kb and kb.chunk_count > 0:
        query = f"{unit['name']} {unit['description']} {target['soc']} registers"
        context = kb.format_context(query, n_results=8)
        if context:
            prompt_parts.append("")
            prompt_parts.append(context)

    return "\n".join(prompt_parts)


def build_engineer_revision_prompt(
    unit: dict,
    target: dict,
    previous_engineer_output: str,
    verifier_feedback: str,
    kb: KnowledgeBase | None = None,
) -> str:
    """Build a revision prompt with verifier feedback for the engineer."""
    rag_context = ""
    if kb and kb.chunk_count > 0:
        query = f"{unit['name']} {unit['description']} {target['soc']} registers"
        context = kb.format_context(query, n_results=8)
        if context:
            rag_context = f"\n\n{context}"

    return (
        f"# Revision Required: {unit['name']}\n\n"
        f"## Target\n"
        f"- Board: {target['board']}\n"
        f"- SoC: {target['soc']}\n"
        f"- Architecture: {target['architecture']}\n"
        f"- CPU Core: {target['cpu_core']}\n\n"
        f"## Original Assignment\n"
        f"{unit['description']}\n\n"
        f"## Your Previous Output\n"
        f"Your previous artifacts were reviewed by a verification engineer who "
        f"cross-checked them against the vendor reference manual. "
        f"The verifier found mismatches that need to be corrected.\n\n"
        f"### Verifier Feedback\n"
        f"{verifier_feedback}\n\n"
        f"## Your Task\n"
        f"Read the verifier's feedback carefully. For each mismatch:\n"
        f"1. Check the reference documentation below to confirm the correct value.\n"
        f"2. Update your .repl and/or C# peripheral model to use the verified value.\n"
        f"3. Output complete, revised artifact files (not diffs).\n\n"
        f"Do NOT invent values. Use only the reference documentation provided."
        f"{rag_context}"
    )


def build_unit_verifier_prompt(
    unit: dict,
    board: str,
    user_prompt: str,
    kb: KnowledgeBase | None = None,
) -> str:
    """Build the prompt for a verifier checking a single work unit."""
    rag_context = ""
    if kb and kb.chunk_count > 0:
        query = (
            f"{unit['name']} base address size IRQ registers "
            f"{unit.get('description', '')}"
        )
        context = kb.format_context(query, n_results=8)
        if context:
            rag_context = f"\n\n{context}"

    return (
        f"Verify the emulation artifacts for work unit `{unit['name']}` "
        f"on board `{board}`.\n\n"
        f"## Original request\n{user_prompt}\n\n"
        f"## Artifacts to verify\n"
        f"- Platform description: `output/{board}/*.repl` (entries related to {unit['name']})\n"
        f"- Custom peripherals: `output/{board}/*.cs` (related to {unit['name']})\n\n"
        f"## Your tasks\n"
        f"1. Read the artifacts produced for this work unit from `output/{board}/`.\n"
        f"2. For every peripheral in this unit, cross-check its base address, size, "
        f"and IRQ number against the reference documentation below.\n"
        f"3. Produce a `verification_report.json` with per-peripheral verdicts.\n"
        f"4. If all peripherals are verified, generate a validated `.resc` snippet "
        f"for this unit.\n"
        f"5. Write any doubt-log entries for unresolvable mismatches.\n\n"
        f"Write all outputs to `output/{board}/`."
        f"{rag_context}"
    )


def build_tester_prompt(board: str) -> str:
    """Build the prompt for the test aggregator."""
    return (
        f"Run the emulation test workflow for board `{board}`.\n\n"
        f"## Artifacts available\n"
        f"- Verified execution script: `output/{board}/run.resc`\n"
        f"- Verification report: `output/{board}/verification_report.json`\n"
        f"- Platform files: `output/{board}/*.repl`\n"
        f"- Peripheral models: `output/{board}/*.cs`\n"
        f"- Doubt log: `output/{board}/doubt_log.json` (if present)\n"
        f"- Sample firmware: `samples/`\n\n"
        f"## Your tasks\n"
        f"1. Read `verification_report.json` to identify expected-failure peripherals.\n"
        f"2. Build each sample under `samples/` for board `{board}`.\n"
        f"3. Write Robot Framework test suites in `output/{board}/tests/`.\n"
        f"4. Run the tests and classify failures as expected vs unexpected.\n"
        f"5. Write `output/{board}/tests/report.txt` with the results summary."
    )


def build_binloop_revision_prompt(report: dict, board: str) -> str:
    """Build the engineer revision prompt from a binary-loop failure report."""
    evidence = "\n".join(f"- {e}" for e in report.get("evidence", []) or [])
    return (
        f"# Binary-in-the-loop failure: `{report['binary']}` on `{board}`\n\n"
        f"A prebuilt firmware was run headless under Renode against your generated "
        f"model and FAILED with an empirical symptom. Fix the responsible peripheral.\n\n"
        f"## Symptom (taxonomy)\n{report.get('symptom')}\n\n"
        f"## Evidence (Renode log)\n{evidence or '- (none captured)'}\n\n"
        f"## Faulting address\n{report.get('address')}\n"
        f"## Likely peripheral\n{report.get('peripheral_guess')}\n\n"
        f"## How to fix\n{report.get('instruction')}\n\n"
        f"Query the `tlde-kb` MCP (`get_peripheral`/`get_register`) for the correct "
        f"grounded value, update the `.repl` and/or C# model under `output/{board}/`, "
        f"and emit COMPLETE files (not diffs). The binary tells you WHAT is wrong; "
        f"tlde-kb tells you the correct VALUE — never hack a value just to pass."
    )


def build_classifier_prompt(classification) -> str:
    """Build the failure-classifier prompt from an ambiguous Classification."""
    return (
        "Classify this headless Renode run. Respond with ONLY the JSON object "
        "described in your instructions.\n\n"
        f"## Heuristic guess\ncategory={classification.category} "
        f"model_defect={classification.model_defect}\n\n"
        f"## Renode log (tail)\n{classification.log}\n\n"
        f"## Console UART (tail)\n{classification.uart or '(empty)'}\n"
    )


def parse_work_plan(response: str) -> dict:
    """Parse the manager's JSON response.

    Handles: raw JSON, markdown code fences, or JSON embedded in prose.
    """
    text = response.strip()

    # Try raw JSON first
    if text.startswith("{"):
        try:
            return _validate_plan(json.loads(text))
        except json.JSONDecodeError:
            pass

    # Strip markdown code fences
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            try:
                return _validate_plan(json.loads(match.group(1)))
            except json.JSONDecodeError:
                pass

    # Last resort: find the outermost { ... } in the response
    start = text.find("{")
    if start != -1:
        # Find matching closing brace by counting nesting
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return _validate_plan(json.loads(text[start : i + 1]))
                    except json.JSONDecodeError:
                        break

    print("[ERROR] Could not extract JSON from manager response.")
    print(f"[ERROR] Response starts with: {response[:300]}")
    raise ValueError("Manager response is not valid JSON")


def _validate_plan(plan: dict) -> dict:
    """Basic validation of the work plan structure."""
    if "target" not in plan or "work_units" not in plan:
        raise ValueError("Missing 'target' or 'work_units' keys")
    return plan


def _cli():
    """CLI entry point for `tlde` command."""
    try:
        asyncio.run(main())
    except ModelUnavailableError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
