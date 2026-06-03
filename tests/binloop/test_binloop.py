"""Binary-in-the-loop tests.

Deterministic parts (metadata, .resc generation, the failure taxonomy, and the
self-correction loop incl. the regression guard) run with no Renode via an
injected evaluator. One opt-in test exercises the real headless Renode runner if
Renode is installed (skipped otherwise), proving the runner/resc/classify path.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from tlde import settings as S
from tlde.binloop import classify, discover
from tlde.binloop.classify import Classification
from tlde.binloop.loop import run_binary_loop
from tlde.binloop.metadata import load_meta
from tlde.binloop.resc import generate_resc
from tlde.binloop.runner import RunResult, find_renode, run


def _write_binary(root: Path, board: str, name: str, fmt="elf", extra="") -> Path:
    d = root / board / name
    d.mkdir(parents=True)
    (d / f"zephyr.{fmt}").write_bytes(b"\x7fELF" if fmt == "elf" else b"x")
    (d / "meta.toml").write_text(textwrap.dedent(f"""\
        [binary]
        board = "{board}"
        soc = "ACME_MCU"
        file = "zephyr.{fmt}"
        format = "{fmt}"
        console_uart = "uart0"
        {extra}
        [success]
        tier = 1
        virtual_time_budget_s = 5
    """))
    return d


def test_metadata_discover_and_parse(tmp_path):
    _write_binary(tmp_path, "acme_board", "hello")
    _write_binary(tmp_path, "acme_board", "blinky", fmt="hex")
    metas = discover(tmp_path, "acme_board")
    assert {m.name for m in metas} == {"hello", "blinky"}
    m = next(x for x in metas if x.name == "hello")
    assert m.format == "elf" and m.console_uart == "uart0" and m.success.tier == 1


def test_meta_bin_load_address(tmp_path):
    d = tmp_path / "b" / "raw"; d.mkdir(parents=True)
    (d / "fw.bin").write_bytes(b"x")
    (d / "meta.toml").write_text(textwrap.dedent("""\
        [binary]
        file = "fw.bin"
        format = "bin"
        load_address = "0x8000"
    """))
    m = load_meta(d / "meta.toml")
    assert m.load_address == 0x8000


def test_resc_generation(tmp_path):
    _write_binary(tmp_path, "acme", "hello")
    meta = discover(tmp_path, "acme")[0]
    text = generate_resc(meta, repl_files=["/o/acme.repl"], cs_files=["/o/W.cs"],
                         uart_log_path="/o/uart.log")
    assert 'mach create "m"' in text
    assert "include @/o/W.cs" in text
    assert "machine LoadPlatformDescription @/o/acme.repl" in text
    assert "sysbus LoadELF @" in text and "zephyr.elf" in text
    assert "sysbus.uart0 CreateFileBackend @/o/uart.log true" in text
    assert 'emulation RunFor "00:00:05"' in text and text.strip().endswith("quit")


# -- failure taxonomy --------------------------------------------------------

def _meta(tmp_path, tier=1, markers=None):
    extra = ""
    _write_binary(tmp_path, "b", "n")
    m = discover(tmp_path, "b")[0]
    m.success.tier = tier
    if markers:
        m.success.expect_console = markers
    return m


@pytest.mark.parametrize("log,expected", [
    ("[WARNING] sysbus: WriteToUnmapped at 0x40002500", "unmapped_sysbus_access"),
    ("[WARNING] uart0: Tag 'BAUDRATE' not handled", "unimplemented_register"),
    ("[ERROR] could not find peripheral at 0x50000000", "peripheral_not_registered"),
    ("[ERROR] access at 0x40001500 is out of range", "wrong_size_or_range"),
    ("[ERROR] cpu: HardFault escalated", "cpu_fault_or_boot"),
])
def test_classify_model_defects(tmp_path, log, expected):
    m = _meta(tmp_path)
    c = classify(RunResult(ran=True, log=log), m)
    assert c.model_defect and c.category == expected and not c.passed


def test_classify_address_and_peripheral(tmp_path):
    m = _meta(tmp_path)
    c = classify(RunResult(ran=True, log="WriteToUnmapped at 0x40002500"), m,
                 address_to_peripheral=lambda a: "WIDGET1" if a == 0x40002500 else None)
    assert c.address == 0x40002500 and c.peripheral_guess == "WIDGET1"


def test_classify_pass_tier1(tmp_path):
    m = _meta(tmp_path)
    c = classify(RunResult(ran=True, log="[INFO] Machine started.\n[INFO] Disposed."), m)
    assert c.passed and c.tier_reached == 1 and not c.model_defect


def test_classify_tier2_markers(tmp_path):
    m = _meta(tmp_path, tier=2, markers=["Hello ACME"])
    ok = classify(RunResult(ran=True, log="[INFO] started", uart="Hello ACME world"), m)
    assert ok.passed and ok.tier_reached == 2
    miss = classify(RunResult(ran=True, log="[INFO] started", uart="nothing"), m)
    assert not miss.passed and miss.tier_reached == 1


def test_classify_spin_on_timeout(tmp_path):
    m = _meta(tmp_path)
    c = classify(RunResult(ran=True, log="[INFO] started", timed_out=True), m)
    assert c.category == "wrong_reset_value_spin" and c.ambiguous


def test_classify_did_not_run(tmp_path):
    m = _meta(tmp_path)
    c = classify(RunResult(ran=False, error="renode not found"), m)
    assert not c.passed and not c.model_defect


def test_classify_run_error_is_not_a_pass(tmp_path):
    # Renode prints command/load errors without a [LEVEL] prefix; these must NOT
    # be mistaken for a clean tier-1 pass (regression: malformed ELF false-pass).
    m = _meta(tmp_path)
    log = ("[INFO] System bus created.\n"
           "There was an error executing command 'sysbus LoadELF @x.elf'\n"
           "Error while loading ELF: Could not load ELF from path")
    c = classify(RunResult(ran=True, log=log), m)
    assert not c.passed and not c.model_defect and c.category is None
    assert "run error" in c.summary


# -- self-correction loop (injected evaluator; no Renode) --------------------

def _cfg(tmp_path):
    cfg = S.Settings()
    cfg.binaries.dir = str(tmp_path / "bins")
    cfg.binaries.max_attempts = 3
    return cfg


def test_loop_detection_only(tmp_path):
    binroot = tmp_path / "bins"
    _write_binary(binroot, "acme", "hello")
    out = tmp_path / "out"; out.mkdir(); (out / "acme.repl").write_text("// repl")
    cfg = _cfg(tmp_path)

    def ev(meta, ctx):
        return Classification(passed=False, tier_reached=0, model_defect=True,
                              category="unmapped_sysbus_access", summary="unmapped")

    reports = asyncio.run(run_binary_loop(cfg, "acme", str(out), evaluate_fn=ev))
    assert len(reports) == 1 and not reports[0].passed
    assert reports[0].residual_category == "unmapped_sysbus_access"
    assert reports[0].attempts == 1  # detection only (no engineer_revise)


def test_loop_self_correction(tmp_path):
    binroot = tmp_path / "bins"
    _write_binary(binroot, "acme", "hello")
    out = tmp_path / "out"; out.mkdir(); (out / "acme.repl").write_text("// repl")
    cfg = _cfg(tmp_path)

    state = {"fixed": False}

    def ev(meta, ctx):
        if state["fixed"]:
            return Classification(passed=True, tier_reached=1, model_defect=False, summary="pass")
        return Classification(passed=False, tier_reached=0, model_defect=True,
                              category="unmapped_sysbus_access", summary="unmapped")

    async def revise(report):
        state["fixed"] = True
        return {"WIDGET1"}

    reports = asyncio.run(run_binary_loop(cfg, "acme", str(out),
                                          engineer_revise=revise, evaluate_fn=ev))
    assert reports[0].passed and reports[0].attempts >= 2
    assert "WIDGET1" in reports[0].changed_peripherals


def test_loop_regression_guard_rolls_back(tmp_path):
    binroot = tmp_path / "bins"
    _write_binary(binroot, "acme", "good")    # passes initially
    _write_binary(binroot, "acme", "bad")     # fails; "fixing" it breaks `good`
    out = tmp_path / "out"; out.mkdir(); (out / "acme.repl").write_text("// repl")
    cfg = _cfg(tmp_path)

    broke = {"applied": False}

    def ev(meta, ctx):
        if meta.name == "good":
            # good regresses once a fix has been applied
            return (Classification(passed=False, tier_reached=0, model_defect=True,
                                   category="unmapped_sysbus_access", summary="regressed")
                    if broke["applied"] else
                    Classification(passed=True, tier_reached=1, model_defect=False, summary="ok"))
        # bad: fails, then passes after the fix is applied
        return (Classification(passed=True, tier_reached=1, model_defect=False, summary="ok")
                if broke["applied"] else
                Classification(passed=False, tier_reached=0, model_defect=True,
                               category="unmapped_sysbus_access", summary="unmapped"))

    async def revise(report):
        broke["applied"] = True
        return {"SHARED"}

    reports = {r.name: r for r in asyncio.run(
        run_binary_loop(cfg, "acme", str(out), engineer_revise=revise, evaluate_fn=ev))}
    assert reports["bad"].regressed is True  # fix rolled back because `good` regressed


# -- real Renode (opt-in) ----------------------------------------------------

@pytest.mark.skipif(find_renode() is None, reason="renode not installed")
def test_runner_real_renode(tmp_path):
    repl = tmp_path / "p.repl"
    repl.write_text(textwrap.dedent("""\
        flash: Memory.MappedMemory @ sysbus 0x0
            size: 0x40000
        sram: Memory.MappedMemory @ sysbus 0x20000000
            size: 0x10000
        nvic: IRQControllers.NVIC @ sysbus 0xE000E000
            -> cpu@0
        cpu: CPU.CortexM @ sysbus
            cpuType: "cortex-m4"
            nvic: nvic
    """))
    resc = tmp_path / "run.resc"
    resc.write_text(textwrap.dedent(f"""\
        mach create "t"
        machine LoadPlatformDescription @{repl}
        emulation RunFor "00:00:00.010"
        quit
    """))
    res = run(str(resc), wall_timeout_s=120)
    assert res.ran and "Machine" in res.log  # Renode actually executed the script
