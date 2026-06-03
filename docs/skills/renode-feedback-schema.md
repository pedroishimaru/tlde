# Skill: Renode Verification & Feedback Schemas

## Purpose

Define the structured JSON contracts exchanged between the verification engineer,
the firmware emulation engineer, and the test aggregator: the
`verification_report.json`, the `doubt_log.json`, and the engineer-facing
revision feedback. These schemas make verification machine-checkable and ensure
every claim is **grounded in a cited source fact**, never the model's priors.

## Grounding rule (read first)

Every address, size, offset, reset value, bit-field, and IRQ number MUST be
backed by a citation obtained from the **tlde-kb MCP** (`get_peripheral`,
`get_register`, `get_memory_map`, `get_pinmap`, `search_docs`) — i.e. from the
SVD/DTS/header/PDF the project was given. A value with no citation is treated as
ungrounded and must be reported as a defect, not silently accepted. The binary
loop (when present) tells you *what* is wrong; the cited source tells you the
*correct value*. Never invent a value to make a check or a test pass.

## verification_report.json

Produced by the verification engineer for the peripheral(s) it checked.

```json
{
  "target": "<board-name>",
  "repl_file": "<path>",
  "verified_against": ["<source> <edition/rev>", "..."],
  "peripherals": [
    {
      "name": "WIDGET0",
      "repl_base_addr": "0x40001000",
      "doc_base_addr":  "0x40001000",
      "repl_size": "0x1000",
      "doc_size":  "0x1000",
      "repl_irq": 5,
      "doc_irq":  5,
      "verdict": "verified | mismatch_fixed | mismatch_escalated | unverifiable",
      "citations": ["<source>:<section/page> (source_type, tier N)"],
      "notes": ""
    }
  ],
  "missing_tier1": [],
  "summary": {
    "total": 0, "verified": 0,
    "mismatch_fixed": 0, "mismatch_escalated": 0, "unverifiable": 0
  }
}
```

Verdicts:
- `verified` — repl value matches a cited source fact.
- `mismatch_fixed` — repl disagreed with the cited source; the engineer must fix it.
- `mismatch_escalated` — sources conflict or are ambiguous; needs human review.
- `unverifiable` — no source fact found to check against (record what was searched).

The pipeline treats a unit as **passed** only when `mismatch_fixed == 0` and
`mismatch_escalated == 0`.

## doubt_log.json

Records unresolved or low-confidence findings for human review.

```json
{
  "entries": [
    {
      "peripheral": "WIDGET0",
      "field": "CTRL.MODE reset value",
      "issue": "SVD and reference manual disagree",
      "candidates": [
        {"value": "0x0", "citation": "acme.svd:WIDGET0.CTRL (svd, tier 1)"},
        {"value": "0x2", "citation": "rm.pdf p.214 Table 8-3 (pdf, tier 3)"}
      ],
      "resolution": "higher trust tier (svd) chosen; flagged",
      "needs_human": true
    }
  ]
}
```

Conflict policy: prefer the lower (more trusted) `trust_tier`; always log the
conflict and both citations. Escalate (`needs_human: true`) when tiers tie or the
discrepancy is safety-relevant.

## Revision feedback (verifier → engineer)

When a unit fails, the engineer receives the `verification_report.json` plus a
short instruction. For each `mismatch_fixed`/`mismatch_escalated` entry the
engineer must:
1. Re-query tlde-kb (`get_register`/`get_peripheral`) to confirm the correct value.
2. Update the `.repl` / C# model to the cited value.
3. Re-emit complete artifact files (not diffs), keeping the citation comment.

## Failure report (binary loop → engineer), when present

The binary-in-the-loop stage emits, per model defect:

```json
{
  "binary": "<name>",
  "symptom": "unmapped_sysbus_access | unimplemented_register | peripheral_not_registered | wrong_reset_value_spin | missing_or_miswired_irq | wrong_size_or_range",
  "evidence": ["<renode log line>", "PC stalled at 0x... for <vt>"],
  "address": "0x40002500",
  "peripheral_guess": "WIDGET1",
  "instruction": "Look up the correct value in tlde-kb and re-ground the fix; do NOT hack a value to pass."
}
```

The symptom says *what* is wrong; the fix's value MUST come from a tlde-kb
citation. Changes without a citation are rejected (overfitting guard).
