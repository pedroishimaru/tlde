# tlde — Too Long Didn't Emulate

> Point it at a datasheet (and ideally an SVD). Get a working, **source-grounded** Renode emulation.

**tlde** is a multi-agent pipeline that turns MCU vendor inputs (CMSIS-SVD, Zephyr devicetree, vendor headers, reference-manual/schematic PDFs) into a complete [Renode](https://renode.io/) emulation package — platform descriptions, C# peripheral models, execution scripts, Robot Framework tests, and a verification report.

Every value the agents emit is **grounded in a cited source fact**, not the model's prior knowledge. If the inputs can't be parsed into a trustworthy model, the run **fails loudly** instead of silently hallucinating a register map.

---

## How it works

```
$ tlde "Emulate the nRF52833 micro:bit v2"        # sources come from tlde.toml

 Phase 0 · Ingest  (FAIL-CLOSED)        SVD ▸ DTS ▸ vendor header ▸ PDF   ─┐
   + 0b · Vision (pinouts/schematics/bit-field figures → structured JSON)  │  cached, page-anchored
   + optional web research (autonomous, trust-allowlisted)                 ├─►  DatasheetModel
                                                                           │   + hybrid-retrieval KB
                              coverage gate: corrupt / zero-yield / low ───┘   served via  tlde-kb MCP
                                          coverage  ⇒  ABORT
            │
 Phase 1 · Manager (opus)        queries tlde-kb ─►  JSON work plan (one unit per peripheral)
            │
 Phase 2 · Engineer ⇄ Verifier   grounded + cited via tlde-kb; ≤ verify_retries; parallel (capped)
            │                     .repl · .cs · run.resc · verification_report.json
 Phase 3 · Binary-in-the-loop    prebuilt ELF/HEX ─► headless Renode ─► classify failure
            │                     ─► re-grounded fix (regression + overfitting guarded)
 Phase 4 · Tester (samples)      west build + Robot tests  (primary functional gate)
            │
 output/<board>/                 *.repl · *.cs · run.resc · verification_report.json ·
                                 binloop_report.json · tests/        (+ .tlde_cache/)
```

The doc-grounded **Verifier** is authoritative for value correctness; the **samples Tester** is the primary functional gate; the **binary loop complements** them with empirical signal (it says *what* is wrong — the docs say the correct *value*).

### Agents

| Agent | Role | Grounding |
|---|---|---|
| `firmware_emulation_manager` | Decomposes the MCU into a topologically-sorted JSON work plan (one unit per peripheral family). | `tlde-kb` MCP |
| `fw_emu_eng` | Implements one work unit: the `.repl` entry + any C# peripheral model. Iterates on verifier + binary-loop feedback. | `tlde-kb` MCP |
| `fw_verif_eng` | Cross-checks the `.repl`/`.cs` against the grounded model/SVD. Per-peripheral verdicts (`verified`, `mismatch_fixed`, `mismatch_escalated`, `unverifiable`) with citations; emits `run.resc`. | `tlde-kb` MCP |
| `emu_test_agg` | Builds Zephyr samples, writes + runs Robot tests, classifies expected vs unexpected failures. | — |
| `fw_failure_classifier` | Adjudicates ambiguous binary-loop runs (model defect vs firmware/harness), citing log lines. | `tlde-kb` MCP |

Models are **per-role** and configurable (see [Configuration](#configuration)); they no longer share one global override.

---

## Grounding: the `tlde-kb` MCP

Phase 0 builds a cached, page-anchored **`DatasheetModel`** (memory map, peripherals, registers, bit-fields, board connectivity) by merging sources in trust order:

```
SVD (tier 1) ▸ DTS (tier 2) ▸ vendor header (tier 1) ▸ upstream Renode ▸ reference-manual PDF (tier 3) ▸ vision (tier 3)
```

Higher-trust sources win on conflict; lower ones fill gaps and add confirmation citations. The model + a hybrid (BM25 + dense + optional rerank) retrieval index are served to agents through a local **`tlde-kb` MCP server** — so agents request *structured slices with citations* (`get_peripheral`, `get_register`, `get_memory_map`, `get_pinmap`, `search_docs`) instead of re-reading whole PDFs. This replaces the old `pdf-reader` MCP.

**Fail-closed by default:** a corrupt/zero-yield source or coverage below `ingest.min_coverage` aborts the run (configurable to `quarantine`/`warn`).

---

## Quickstart

### Requirements

- Python 3.12+
- An LLM backend — either the authenticated [Copilot CLI](https://githubnext.com/projects/copilot-cli/) (`copilot auth login`, the default), **or** a BYOK provider key in `.env`.
- [Renode](https://renode.io/) on `$PATH` (or `/opt/renode/renode`) — for the binary-in-the-loop stage and `renode-test`.
- [west](https://docs.zephyrproject.org/latest/develop/west/) + Zephyr SDK — only for the samples-build Tester (Phase 4); not needed for the prebuilt binary loop.
- Inputs: a reference-manual PDF, and (strongly recommended for precise grounding) a **CMSIS-SVD** and/or resolved **Zephyr DTS**.

### Install

```bash
pip install -e .     # pulls pydantic, cmsis-svd, pdfplumber, devicetree, rank-bm25, mcp, sentence-transformers, …
```

### Install skills (recommended)

Skills pre-load Renode domain knowledge into each agent. Install them once to `~/.copilot/skills/` (auto-includes any new skill under `docs/skills/`):

```bash
for skill in docs/skills/*.md; do
  name=$(basename "$skill" .md)
  mkdir -p ~/.copilot/skills/$name
  cat > ~/.copilot/skills/$name/SKILL.md << FRONT
---
name: $name
description: "$(head -3 $skill | tail -1)"
---
FRONT
  cat "$skill" >> ~/.copilot/skills/$name/SKILL.md
done
```

### Run

```bash
tlde "Emulate the nRF52833 micro:bit v2"
# flags: [--config tlde.toml] [--source PATH_OR_URL ...] [--provider NAME] [--model NAME]
#        [--plan output/work_plan.json]   # resume from a cached work plan
```

Outputs land in `output/<board>/`; the grounded model + retrieval index are cached under `.tlde_cache/`.

---

## Configuration

Everything is driven by a declarative **`tlde.toml`** (see the committed default). Precedence, highest first:

```
CLI flags  >  environment (incl. .env)  >  tlde.toml  >  built-in defaults
```

```toml
[target]   board="bbc_microbit_v2"; soc="nRF52833"; arch="arm"; cpu="cortex-m4f"

[sources]  reference_manual="docs/nrf52833_rm.pdf"; schematic="docs/microbit_v2_schematic.pdf"
           # svd="inputs/nrf52833.svd"; dts="inputs/microbit_v2.dts"
           precedence=["svd","dts","vendor_header","renode_upstream","reference_manual"]

[research] mode="autonomous"        # autonomous | approval | off  (trust-allowlisted)
[ingest]   strictness="fail_closed" # fail_closed | quarantine | warn ; min_coverage=0.7 ; vision="auto"
[binaries] dir="target_binaries"; run_loop=true; max_attempts=4; virtual_time_budget_s=30
[testing]  samples_build=true
[concurrency] max_parallel_units=4; verify_retries=3

[models]   manager="claude-opus-4.6"; engineer="claude-sonnet-4.6"; verifier="claude-sonnet-4.6"
           tester="claude-sonnet-4.6"; failure_classifier="claude-sonnet-4.6"
           vision="claude-sonnet-4.6"; embedder="BAAI/bge-small-en-v1.5"; reranker="BAAI/bge-reranker-base"

[providers] default="github"        # per-role override allowed, e.g. vision="anthropic"
```

### Providers & secrets (BYOK)

Supported: `github` (Copilot, default — no key), `openrouter`, `anthropic`, `openai`, `azure`, `ollama`. Put keys in `.env` (auto-loaded, gitignored) — **never commit secrets**:

```bash
echo 'OPENROUTER_API_KEY=sk-or-...' >> .env
```

Per-role overrides via env (`TLDE_<ROLE>_MODEL`, `TLDE_<ROLE>_PROVIDER`) or the global `TLDE_MODEL` / `TLDE_PROVIDER` still work.

**OpenRouter with open-weight models** — a ready profile lives at [`examples/tlde.openrouter.toml`](examples/tlde.openrouter.toml) (GLM-5, Qwen3-Coder, Kimi-K2.6, DeepSeek-V4-Flash, Qwen3-VL):

```bash
echo 'OPENROUTER_API_KEY=sk-or-...' >> .env
make openrouter        # == tlde "..." --config examples/tlde.openrouter.toml
```

### Vision

Figure extraction runs through the configured vision-capable model via the Copilot SDK's image attachments — provider-agnostic (Copilot/Anthropic/OpenAI/Azure/OpenRouter/Ollama). With `ingest.vision="auto"` it self-disables when no vision model is available.

### Autonomous web research

When `[research].mode != "off"` and a machine-readable input (SVD/DTS/header) is missing, tlde can discover + fetch it from a configurable **trust allowlist** (vendor → ARM/CMSIS → Zephyr/Renode → standards → distributor → community), recording provenance + a trust tier per artifact and guarding against wrong-part / version-mismatch / paywalled results. It is offline-safe: with no search backend wired it no-ops with a note. `mode="approval"` requires human sign-off before a source is trusted.

---

## Binary-in-the-loop

Drop already-built Zephyr artifacts under `target_binaries/<board>/<name>/` (`zephyr.elf|hex|bin` + `meta.toml`) — **no toolchain needed**. The stage generates a `.resc`, runs Renode headless bounded by *virtual* time, and classifies any failure into a model-defect taxonomy (`unmapped_sysbus_access`, `unimplemented_register`, `peripheral_not_registered`, `wrong_reset_value_spin`, `missing_or_miswired_irq`, `wrong_size_or_range`). Model defects route a structured report back to the engineer, who **re-grounds the fix in `tlde-kb`** — guarded against overfitting (citations required) and regressions (touched binaries re-run; a regressing fix is rolled back). See [`target_binaries/README.md`](target_binaries/README.md).

---

## Project structure

```
tlde/
├── src/tlde/
│   ├── pipeline/firmware_emulation.py   # phase orchestrator (0/0b → 1 → 2 → 3 → 4)
│   ├── settings.py                      # tlde.toml loader + per-role resolution
│   ├── ingest/                          # Phase 0: grounded DatasheetModel + tlde-kb MCP
│   │   ├── datasheet_model.py  build.py  merge.py  coverage.py  cache.py
│   │   ├── svd.py  dts.py  headers.py  pdf.py  vision.py
│   │   └── mcp_server.py  mcp_config.py
│   ├── research/                        # autonomous-in-allowlist web research
│   │   └── policy.py  discover.py  fetch.py  rank.py
│   ├── binloop/                         # binary-in-the-loop self-correction
│   │   └── metadata.py  resc.py  runner.py  classify.py  loop.py  report.py
│   ├── agents/                          # firmware_emulation_manager, fw_emu_eng,
│   │   └── …                            #   fw_verif_eng, emu_test_agg, fw_failure_classifier
│   ├── agent.py  config.py  providers.py  rag.py  observability.py
├── prompts/                             # one system prompt per agent
├── docs/skills/                         # Renode domain-knowledge skills
├── samples/                             # Zephyr sample apps (samples-build Tester)
├── target_binaries/                     # prebuilt firmware for the binary loop
├── tests/                               # pytest suite (ingest / research / binloop)
├── tlde.toml                            # default project config
├── examples/tlde.openrouter.toml        # open-weight OpenRouter profile
└── .env.example
```

---

## Output artefacts

| File | Produced by | Description |
|---|---|---|
| `output/<board>/*.repl` / `*.cs` | `fw_emu_eng` | Renode platform description + C# peripheral models |
| `output/<board>/run.resc` | `fw_verif_eng` | Validated Renode execution script |
| `output/<board>/verification_report.json` | `fw_verif_eng` | Per-peripheral verdicts with grounded citations |
| `output/<board>/doubt_log.json` | `fw_verif_eng` | Escalated / unresolvable mismatches |
| `output/<board>/binloop_report.json` | binary loop | Per-binary: boot reached, markers hit, residual defects, attempts |
| `output/<board>/tests/*.robot` · `report.txt` | `emu_test_agg` | Robot suites + pass/fail summary |
| `.tlde_cache/` | Phase 0 | Cached DatasheetModel + retrieval corpus (gitignored) |

---

## Walkthroughs

**Add a new board** — drop inputs in `inputs/` (or let research fetch them) + prebuilt firmware in `target_binaries/<board>/`, edit `tlde.toml` `[target]`/`[sources]`, run `tlde "Emulate <board>"`. No code changes.

**Switch to OpenRouter (open weights)** — `echo OPENROUTER_API_KEY=… >> .env`, then `make openrouter` (or `--config examples/tlde.openrouter.toml`).

**New prebuilt-binary set** — drop `target_binaries/<board>/<name>/{*.elf, meta.toml}` (golden markers optional) and run; the loop generates `.resc`, runs headless Renode, and feeds re-grounded fixes back.

---

## Development

### Tests

```bash
uv run pytest tests/ -q     # ingest, mcp server, vision, research, binary loop
```

Fixtures are synthetic/generic (no vendor-specific data); one test exercises real Renode if installed (skipped otherwise).

### Adding an agent / skill

- Agent: add `src/tlde/agents/<name>.py` (subclass `AgentConfig`) + `prompts/<name>.txt`; it auto-registers as `AGENTS["<name>"]`. Map its role→model in `settings.ROLE_BY_AGENT` + `[models]`.
- Skill: add `docs/skills/<name>.md` and re-run the install loop; reference it in the agent's `skills` list.

### Permission model

`agent.py` gates shell commands. The default handler approves requests (so agents can `mkdir`/write artifacts); the **Tester runs under a restricted allowlist** instead of approve-all:

| Handler | Allowed shell |
|---|---|
| `_approve_all_handler` (default) | all requests |
| `_test_permission_handler` (Tester) | `mkdir [-p]`, `make`, `west build`, `renode`, `renode-test`, `python -m robot` |

The binary-loop runs Renode directly via a fixed-argument subprocess (not an agent shell tool).
